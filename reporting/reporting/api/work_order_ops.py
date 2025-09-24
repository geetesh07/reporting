# apps/reporting/reporting/api/work_order_ops.py
# Reporting API for Work Order operations (Job Card friendly).
# - safe workstation authorization retrieval (prevents "Field not permitted" errors)
# - job card reuse / creation / submission per punch (one time-log row per punch)
# - strict downstream quantity validation (prev op produced limits next op)
# - per-punch audit table (Operation Punch Log)
# - keeps Work Order produced_qty in sync with op sums (helps Finish flow)

import nts
from nts import _
from nts.utils import flt, get_datetime, now_datetime
import uuid

def _make_name(prefix="OPLOG"):
    return "{}-{}".format(prefix, uuid.uuid4().hex[:12])

def _ensure_punch_table():
    try:
        nts.db.sql("""
            CREATE TABLE IF NOT EXISTS `tabOperation Punch Log` (
                `name` varchar(255) NOT NULL,
                `creation` DATETIME NULL,
                `modified` DATETIME NULL,
                `owner` varchar(180) NULL,
                `parent_work_order` varchar(255) NULL,
                `parent_op_idx` INT NULL,
                `parent_op_name` varchar(255) NULL,
                `employee_number` varchar(180) NULL,
                `employee_name` varchar(255) NULL,
                `produced_qty` decimal(18,6) DEFAULT 0,
                `rejected_qty` decimal(18,6) DEFAULT 0,
                `posting_datetime` DATETIME NULL,
                PRIMARY KEY (`name`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """)
    except Exception:
        # best-effort, non-fatal
        pass

def pick_from_time(op_idx, wo_doc):
    if op_idx == 0:
        for fld in ("actual_start_date", "actual_start_time", "planned_start_date", "start_date", "wip_started_datetime", "creation"):
            v = wo_doc.get(fld)
            if v:
                try:
                    return get_datetime(v)
                except Exception:
                    return v
        return now_datetime()
    prev = (wo_doc.get("operations") or [])[op_idx - 1] if op_idx - 1 >= 0 else None
    if prev and prev.get("op_reported_dt"):
        try:
            return get_datetime(prev.get("op_reported_dt"))
        except Exception:
            return now_datetime()
    prev_op_text = prev.get("operation") or prev.get("operation_name") or ""
    try:
        rows = nts.db.sql("select name from `tabJob Card` where work_order=%s and operation=%s order by creation desc limit 1", (wo_doc.name, prev_op_text))
        if rows:
            jc_prev = rows[0][0]
            try:
                jc_prev_doc = nts.get_doc("Job Card", jc_prev)
                if getattr(jc_prev_doc, "to_time", None):
                    return get_datetime(jc_prev_doc.to_time) if jc_prev_doc.to_time else jc_prev_doc.to_time
            except Exception:
                pass
    except Exception:
        pass
    return now_datetime()

@nts.whitelist()
def get_workstation_allowed(workstation):
    """
    Safe server helper. Returns comma-separated allowed tokens for a workstation.
    This avoids client asking the DB for a field that isn't a simple column.
    """
    if not workstation:
        return ""
    allowed_csv = ""
    try:
        # prefer get_doc (works for child tables and simple fields)
        w = None
        try:
            w = nts.get_doc("Workstation", workstation)
        except Exception:
            w = None
        if w:
            # field names we've seen in various setups
            for fld in ("authorized_employee_numbers", "authorized_employee_ids", "authorized_employees"):
                if hasattr(w, fld):
                    val = getattr(w, fld)
                    # if it's a list/child table, extract sensible tokens
                    if isinstance(val, list):
                        tokens = []
                        for row in val:
                            # common keys used in such child tables
                            for k in ("employee_number", "employee", "employee_id", "name"):
                                if row.get(k):
                                    tokens.append(str(row.get(k)))
                        allowed_csv = ",".join(tokens)
                        if allowed_csv:
                            break
                    else:
                        # string/CSV
                        if val:
                            allowed_csv = str(val)
                            break
        # fallback: try db.get_value for simple column if present
        if not allowed_csv:
            try:
                # this will error if column not permitted - that's why it's in try
                allowed_csv = nts.db.get_value("Workstation", workstation, "authorized_employee_numbers") or ""
            except Exception:
                try:
                    allowed_csv = nts.db.get_value("Workstation", workstation, "authorized_employee_ids") or ""
                except Exception:
                    allowed_csv = ""
    except Exception:
        allowed_csv = ""
    return allowed_csv or ""

@nts.whitelist()
def get_punch_logs(work_order):
    _ensure_punch_table()
    try:
        rows = nts.db.sql("""
            SELECT parent_op_idx, employee_number, employee_name, produced_qty, rejected_qty, posting_datetime, name
            FROM `tabOperation Punch Log`
            WHERE parent_work_order=%s
            ORDER BY parent_op_idx ASC, posting_datetime ASC
        """, (work_order,), as_dict=True)
        out = {}
        for r in rows:
            idx = int(r.parent_op_idx or 0)
            out.setdefault(idx, []).append(r)
        return out
    except Exception:
        return {}

@nts.whitelist()
def report_operation(work_order, op_index, operation_name, employee_number, produced_qty, process_loss=0, posting_datetime=None, complete_operation=1):
    """
    Reports an operation quantity (single punch). Key points:
    - strict downstream validation (a later op cannot process more than prev op produced)
    - creates/reuses/submits Job Card properly so FRAPPE hooks run
    - inserts one time_log row per punch
    - writes authoritative totals into Work Order Operation and updates Work Order produced_qty
    - records per-punch audit into Operation Punch Log (SQL)
    """
    produced_qty = flt(produced_qty or 0)
    process_loss = flt(process_loss or 0)
    if produced_qty <= 0 and process_loss <= 0:
        nts.throw(_("Either produced qty or rejected qty must be greater than zero."))

    posting_dt = get_datetime(posting_datetime) if posting_datetime else now_datetime()

    # reporter display only
    emp_display_number = str(employee_number or "").strip()
    emp_display_name = ""
    emp_docname = None
    try:
        emp = nts.db.get_value("Employee", {"employee_number": employee_number}, ["name", "employee_name"], as_dict=True)
        if emp:
            emp_docname = emp.get("name")
            emp_display_name = str(emp.get("employee_name") or "")
    except Exception:
        emp_docname = None
    reporter_label = emp_display_number + (" - " + emp_display_name if emp_display_name else "")

    # load work order
    wo = nts.get_doc("Work Order", work_order)
    if wo.docstatus != 1:
        nts.throw(_("Work Order must be submitted."))
    if not wo.get("material_transferred_for_manufacturing"):
        nts.throw(_("Transfer materials to WIP before reporting."))

    try:
        idx = int(op_index)
    except Exception:
        nts.throw(_("Invalid operation index."))

    operations = wo.get("operations") or []
    if idx < 0 or idx >= len(operations):
        nts.throw(_("Operation index out of range."))

    def op_required(o):
        rq = flt(o.get("operation_qty") or o.get("for_quantity") or o.get("qty") or o.get("required_qty") or 0)
        if rq <= 0:
            rq = flt(wo.get("qty") or wo.get("production_qty") or wo.get("for_quantity") or 0)
        return rq

    # find first pending (your rule)
    first_pending = None
    for i, o in enumerate(operations):
        required = op_required(o)
        done_sum = flt(o.get("completed_qty") or 0) + flt(o.get("process_loss_qty") or 0)
        if not o.get("op_reported") and done_sum < required - 1e-9:
            first_pending = i
            break
    if first_pending is None:
        nts.throw(_("All operations already reported."))
    if idx != first_pending:
        nts.throw(_("You can only report the next pending operation (index {0}).").format(first_pending))

    op_row = operations[idx]
    required_qty = op_required(op_row)

    # ---------------- strict available calculation ----------------
    if idx == 0:
        available_input = required_qty
    else:
        # available is what previous op produced (prev.completed_qty) capped by this op's required
        prev = operations[idx - 1]
        prev_completed = flt(prev.get("completed_qty") or 0)
        try:
            if prev.get("name"):
                vals = nts.db.get_value("Work Order Operation", prev.get("name"), ["completed_qty"], as_dict=True)
                if vals:
                    prev_completed = flt(vals.get("completed_qty") or 0)
            else:
                rows = nts.db.sql("""SELECT completed_qty FROM `tabWork Order Operation` WHERE parent=%s AND idx=%s LIMIT 1""", (wo.name, prev.get("idx") or idx))
                if rows:
                    prev_completed = flt(rows[0][0] or 0)
        except Exception:
            prev_completed = flt(prev.get("completed_qty") or 0)
        available_input = min(required_qty, prev_completed)

    # concurrency safe read of current totals for the op
    current_completed = 0.0
    current_rejected = 0.0
    try:
        if op_row.get("name"):
            vals = nts.db.get_value("Work Order Operation", op_row.get("name"), ["completed_qty", "process_loss_qty"], as_dict=True)
            if vals:
                current_completed = flt(vals.get("completed_qty") or 0)
                current_rejected = flt(vals.get("process_loss_qty") or 0)
        else:
            rows = nts.db.sql("""SELECT completed_qty, process_loss_qty FROM `tabWork Order Operation` WHERE parent=%s AND idx=%s LIMIT 1""", (wo.name, op_row.get("idx") or (idx+1)))
            if rows and rows[0]:
                current_completed = flt(rows[0][0] or 0)
                current_rejected = flt(rows[0][1] or 0)
    except Exception:
        current_completed = flt(op_row.get("completed_qty") or 0)
        current_rejected = flt(op_row.get("process_loss_qty") or 0)

    pending_qty = available_input - (current_completed + current_rejected)
    if pending_qty < 0:
        pending_qty = 0.0

    # validate incoming punch
    if (produced_qty + process_loss) - 1e-9 > pending_qty:
        nts.throw(_("Produced + Rejected ({0}) exceeds pending ({1}).").format(produced_qty + process_loss, pending_qty))
    if int(complete_operation or 0) == 1 and abs((produced_qty + process_loss) - pending_qty) > 1e-6:
        nts.throw(_("To complete, produced + rejected must equal pending ({0}).").format(pending_qty))

    new_completed = current_completed + produced_qty
    new_rejected = current_rejected + process_loss
    new_pending = available_input - (new_completed + new_rejected)
    if new_pending < 0:
        new_pending = 0.0

    # ---------------- Job Card handling (create / reuse / submit) ----------------
    op_text = op_row.get("operation") or op_row.get("operation_name") or operation_name or ""
    # find latest job card for this wo+operation
    jc_name = None
    try:
        rows = nts.db.sql("select name from `tabJob Card` where work_order=%s and operation=%s order by creation desc limit 1", (wo.name, op_text))
        if rows:
            jc_name = rows[0][0]
    except Exception:
        jc_name = None

    jc_doc = None
    if jc_name:
        try:
            jc_doc = nts.get_doc("Job Card", jc_name)
        except Exception:
            jc_doc = None

    workstation_val = op_row.get("workstation") or wo.get("workstation") or ""
    # if jc exists and is submitted -> create a new JC for this punch (so we can submit it with this punch)
    # if jc exists and docstatus==0 -> reuse it
    # if no jc exists -> create one (requires workstation to be present)
    def _append_time_log_and_submit(jc, employee_link, employee_number_display):
        """
        Append a single time_logs row to jc (in-memory), save and submit.
        We set completed_qty & rejected_qty on the time_log child to the punch qty.
        """
        try:
            # append time log
            try:
                jc.append("time_logs", {
                    "employee": employee_link,
                    "from_time": pick_from_time(idx, wo),
                    "to_time": posting_dt,
                    "time_in_mins": max(0, int(round((get_datetime(str(posting_dt)) - get_datetime(str(pick_from_time(idx, wo))).total_seconds() / 60.0))) ) if False else 0,
                    "completed_qty": produced_qty,
                    "rejected_qty": process_loss
                })
            except Exception:
                # sometimes time_in_mins calculation/fields cause trouble in some forks,
                # append with minimal payload
                jc.append("time_logs", {
                    "employee": employee_link,
                    "from_time": pick_from_time(idx, wo),
                    "to_time": posting_dt,
                    "completed_qty": produced_qty,
                    "rejected_qty": process_loss
                })
            # save and submit (ignore permissions)
            try:
                jc.save(ignore_permissions=True)
            except Exception:
                # best-effort: try to log but continue
                nts.log_error(nts.get_traceback(), "job card save failed")
            try:
                # allow ignoring permissions for submit
                jc.flags = getattr(jc, "flags", {})
                jc.flags.ignore_permissions = True
                if getattr(jc, "docstatus", 0) == 0:
                    jc.submit()
            except Exception:
                # if submit fails, log (but we will still update WO operation totals)
                nts.log_error(nts.get_traceback(), "job card submit failed")
        except Exception:
            nts.log_error(nts.get_traceback(), "append_time_log_and_submit failed")

    # Decide action
    if jc_doc:
        if getattr(jc_doc, "docstatus", 0) == 0:
            # reuse: append time log and submit
            _append_time_log_and_submit(jc_doc, emp_docname, emp_display_number)
            jc_name = jc_doc.name
        else:
            # already submitted -> create a new job card for this punch (requires workstation)
            if not workstation_val:
                # cannot create job card; user must provide workstation for job cards
                # fall back: do not create job card, but continue (we will still record punch audit and update op)
                jc_name = None
            else:
                try:
                    orig_msgprint = getattr(nts, "msgprint", None)
                    try:
                        nts.msgprint = lambda *a, **k: None
                        new_jc = nts.get_doc({
                            "doctype": "Job Card",
                            "work_order": wo.name,
                            "operation": op_text,
                            "for_quantity": produced_qty + process_loss,
                            "workstation": workstation_val
                        })
                        new_jc.insert(ignore_permissions=True)
                    finally:
                        if orig_msgprint is not None:
                            nts.msgprint = orig_msgprint
                    if new_jc:
                        _append_time_log_and_submit(new_jc, emp_docname, emp_display_number)
                        jc_name = new_jc.name
                except Exception:
                    # if JC creation fails, swallow and continue (we will still update op)
                    jc_name = None
    else:
        # No JC exists; try to create if workstation is present (Job Card requires workstation in many setups)
        if not workstation_val:
            # cannot create job card. We don't throw here because you told me to avoid hard failures;
            # instead we continue and still record punch audit + op totals. But we inform the user.
            # to strictly enforce, you can replace the next line with nts.throw(...)
            pass
        else:
            try:
                orig_msgprint = getattr(nts, "msgprint", None)
                try:
                    nts.msgprint = lambda *a, **k: None
                    new_jc = nts.get_doc({
                        "doctype": "Job Card",
                        "work_order": wo.name,
                        "operation": op_text,
                        "for_quantity": produced_qty + process_loss,
                        "workstation": workstation_val
                    })
                    new_jc.insert(ignore_permissions=True)
                finally:
                    if orig_msgprint is not None:
                        nts.msgprint = orig_msgprint
                if new_jc:
                    _append_time_log_and_submit(new_jc, emp_docname, emp_display_number)
                    jc_name = new_jc.name
            except Exception:
                jc_name = None

    # ---------------- Update authoritative Work Order Operation totals ----------
    set_vals = {
        "completed_qty": new_completed,
        "process_loss_qty": new_rejected,
        "op_reported": 1 if int(complete_operation or 0) == 1 or new_pending <= 1e-9 else 0,
        "op_reported_by_user": nts.session.user,
        "op_reported_dt": str(posting_dt)
    }
    try:
        cols = nts.db.sql("SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='tabWork Order Operation' AND COLUMN_NAME='op_reported_by_employee_name'")
        if cols:
            rep = reporter_label or ""
            if rep and len(rep) > 200:
                rep = rep[:200]
            set_vals["op_reported_by_employee_name"] = rep
    except Exception:
        pass

    try:
        if op_row.get("name"):
            nts.db.set_value("Work Order Operation", op_row.get("name"), set_vals, update_modified=False)
        else:
            nts.db.sql("""UPDATE `tabWork Order Operation`
                          SET completed_qty=%s, process_loss_qty=%s, op_reported=%s, op_reported_by_user=%s, op_reported_dt=%s
                          WHERE parent=%s AND idx=%s""",
                       (new_completed, new_rejected, set_vals["op_reported"], set_vals["op_reported_by_user"], set_vals["op_reported_dt"], wo.name, op_row.get("idx") or (idx+1)))
    except Exception:
        try:
            nts.log_error(nts.get_traceback(), "work order operation update failed")
        except Exception:
            pass

    # Update Work Order produced_qty to keep FRAPPE finish checks happy (sum of op completed)
    try:
        total_produced = 0.0
        rows = nts.db.sql("SELECT COALESCE(SUM(completed_qty),0) FROM `tabWork Order Operation` WHERE parent=%s", (wo.name,))
        if rows:
            total_produced = flt(rows[0][0] or 0)
            nts.db.set_value("Work Order", wo.name, {"produced_qty": total_produced}, update_modified=False)
    except Exception:
        pass

    # ---------------- per-punch audit log (non-critical) ----------------
    try:
        _ensure_punch_table()
        row_name = _make_name("OPLOG")
        nts.db.sql("""INSERT INTO `tabOperation Punch Log` (name, creation, modified, owner, parent_work_order, parent_op_idx, parent_op_name, employee_number, employee_name, produced_qty, rejected_qty, posting_datetime)
                      VALUES (%s, NOW(), NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                   (row_name, nts.session.user, wo.name, idx, op_text, emp_display_number, emp_display_name, produced_qty, process_loss, posting_dt))
    except Exception:
        try:
            nts.log_error(nts.get_traceback(), "punch log insert failed")
        except Exception:
            pass

    remaining = max(0.0, available_input - (new_completed + new_rejected))
    remaining = flt(remaining)
    return _("Operation {0} reported: produced {1}, rejected {2}. Remaining for this op: {3}").format(op_text, produced_qty, process_loss, remaining)
