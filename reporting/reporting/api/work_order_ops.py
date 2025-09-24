# apps/reporting/reporting/api/work_order_ops.py
# Reporting API for Work Order operations (Job Card friendly).
# - Reuse existing unsubmitted Job Card for partial punches (do NOT submit)
# - Create Job Card only if needed and workstation present (silent)
# - Submit Job Card only when operation completes (complete_operation==1)
# - Avoid double-counting: re-read op totals after JC submit if JC submitted
# - produced_qty on Work Order is set from last operation's completed_qty (clamped to WO qty)
# - Per-punch audit table: tabOperation Punch Log (created if missing)

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
        # ignore if cannot create (e.g. insufficient privileges); punch audit will be best-effort
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
    """Return allowed token CSV for a workstation safely (avoid Field not permitted errors)."""
    if not workstation:
        return ""
    allowed_csv = ""
    try:
        w = None
        try:
            w = nts.get_doc("Workstation", workstation)
        except Exception:
            w = None
        if w:
            # prefer structured child table if present
            for fld in ("authorized_employee_numbers", "authorized_employee_ids", "authorized_employees"):
                if hasattr(w, fld):
                    val = getattr(w, fld)
                    if isinstance(val, list):
                        tokens = []
                        for row in val:
                            for k in ("employee_number", "employee", "employee_id", "name"):
                                if row.get(k):
                                    tokens.append(str(row.get(k)))
                        allowed_csv = ",".join(tokens)
                        if allowed_csv:
                            break
                    else:
                        if val:
                            allowed_csv = str(val)
                            break
        if not allowed_csv:
            try:
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
    """Return punch logs grouped by operation index for UI rendering."""
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
    """Report produced/rejected for a work order operation.

    - produced_qty and process_loss must be numeric.
    - Validation ensures produced+rejected does not exceed pending for this operation (based on prior op completions).
    - Partial punches allowed: appended to existing unsubmitted Job Card's time_logs; JC is NOT submitted until complete_operation==1.
    - If JC submit happens, re-read totals from DB to avoid double-add.
    """
    produced_qty = flt(produced_qty or 0)
    process_loss = flt(process_loss or 0)
    if produced_qty <= 0 and process_loss <= 0:
        nts.throw(_("Either produced qty or rejected qty must be greater than zero."))

    posting_dt = get_datetime(posting_datetime) if posting_datetime else now_datetime()

    # reporter
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
    reporter_label = emp_display_name or emp_display_number or ""

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

    # enforce next-pending rule
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

    # available input is limited by previous operation completed_qty
    if idx == 0:
        available_input = required_qty
    else:
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

    # read existing op totals
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

    # validation
    if (produced_qty + process_loss) - 1e-9 > pending_qty:
        nts.throw(_("Produced + Rejected ({0}) exceeds pending ({1}).").format(produced_qty + process_loss, pending_qty))
    if int(complete_operation or 0) == 1 and abs((produced_qty + process_loss) - pending_qty) > 1e-6:
        nts.throw(_("To complete, produced + rejected must equal pending ({0}).").format(pending_qty))

    # ---------------- Job Card: reuse unsubmitted or create if necessary (silent) --------------
    op_text = op_row.get("operation") or op_row.get("operation_name") or operation_name or ""

    try:
        jc_candidates = nts.db.sql("select name, docstatus from `tabJob Card` where work_order=%s and operation=%s order by creation desc", (wo.name, op_text))
    except Exception:
        jc_candidates = []

    jc_name = None
    if jc_candidates:
        for r in jc_candidates:
            if r and len(r) >= 2 and r[1] == 0:
                jc_name = r[0]
                break
        if not jc_name:
            jc_name = jc_candidates[0][0]

    jc_doc = None
    if jc_name:
        try:
            jc_doc = nts.get_doc("Job Card", jc_name)
        except Exception:
            jc_doc = None

    workstation_val = op_row.get("workstation") or wo.get("workstation") or ""
    jobcard_was_submitted = False

    def append_time_log_in_jc(jc, emp_docname_local, submit_on_complete=False):
        submitted = False
        try:
            tl_values = {
                "employee": emp_docname_local,
                "from_time": pick_from_time(idx, wo),
                "to_time": posting_dt,
                "time_in_mins": 0,
                "completed_qty": produced_qty
            }
            try:
                tl_values["rejected_qty"] = process_loss
            except Exception:
                pass
            jc.append("time_logs", tl_values)
            try:
                jc.save(ignore_permissions=True)
            except Exception:
                nts.log_error(nts.get_traceback(), "job card save failed")
            if submit_on_complete:
                try:
                    jc.flags = getattr(jc, "flags", {})
                    jc.flags.ignore_permissions = True
                    if getattr(jc, "docstatus", 0) == 0:
                        jc.submit()
                        submitted = True
                except Exception:
                    nts.log_error(nts.get_traceback(), "job card submit failed")
        except Exception:
            nts.log_error(nts.get_traceback(), "append_time_log_in_jc failed")
        return submitted

    try:
        if jc_doc:
            if getattr(jc_doc, "docstatus", 0) == 0:
                jobcard_was_submitted = append_time_log_in_jc(jc_doc, emp_docname, submit_on_complete=(int(complete_operation or 0) == 1))
                jc_name = jc_doc.name
            else:
                if workstation_val:
                    orig_msgprint = getattr(nts, "msgprint", None)
                    try:
                        nts.msgprint = lambda *a, **k: None
                        new_jc = nts.get_doc({
                            "doctype": "Job Card",
                            "work_order": wo.name,
                            "operation": op_text,
                            "for_quantity": required_qty,
                            "workstation": workstation_val
                        })
                        new_jc.insert(ignore_permissions=True)
                    finally:
                        if orig_msgprint is not None:
                            nts.msgprint = orig_msgprint
                    if new_jc:
                        jobcard_was_submitted = append_time_log_in_jc(new_jc, emp_docname, submit_on_complete=(int(complete_operation or 0) == 1))
                        jc_name = new_jc.name
                else:
                    # no workstation -> no JC created
                    pass
        else:
            if workstation_val:
                orig_msgprint = getattr(nts, "msgprint", None)
                try:
                    nts.msgprint = lambda *a, **k: None
                    new_jc = nts.get_doc({
                        "doctype": "Job Card",
                        "work_order": wo.name,
                        "operation": op_text,
                        "for_quantity": required_qty,
                        "workstation": workstation_val
                    })
                    new_jc.insert(ignore_permissions=True)
                finally:
                    if orig_msgprint is not None:
                        nts.msgprint = orig_msgprint
                if new_jc:
                    jobcard_was_submitted = append_time_log_in_jc(new_jc, emp_docname, submit_on_complete=(int(complete_operation or 0) == 1))
                    jc_name = new_jc.name
            else:
                # skip JC creation
                pass
    except Exception:
        nts.log_error(nts.get_traceback(), "job card handling failed")

    # ---------------- Update Work Order Operation authoritative totals ----------
    final_completed = None
    final_rejected = None
    try:
        if jobcard_was_submitted:
            # re-read updated totals from DB to avoid double-count
            try:
                if op_row.get("name"):
                    vals = nts.db.get_value("Work Order Operation", op_row.get("name"), ["completed_qty", "process_loss_qty"], as_dict=True)
                    if vals:
                        final_completed = flt(vals.get("completed_qty") or 0)
                        final_rejected = flt(vals.get("process_loss_qty") or 0)
                else:
                    rows = nts.db.sql("""SELECT completed_qty, process_loss_qty FROM `tabWork Order Operation` WHERE parent=%s AND idx=%s LIMIT 1""", (wo.name, op_row.get("idx") or (idx+1)))
                    if rows and rows[0]:
                        final_completed = flt(rows[0][0] or 0)
                        final_rejected = flt(rows[0][1] or 0)
            except Exception:
                final_completed = None
                final_rejected = None
    except Exception:
        final_completed = None
        final_rejected = None

    if final_completed is None or final_rejected is None:
        final_completed = current_completed + produced_qty
        final_rejected = current_rejected + process_loss

    set_vals = {
        "completed_qty": final_completed,
        "process_loss_qty": final_rejected,
        "op_reported": 1 if int(complete_operation or 0) == 1 or (available_input - (final_completed + final_rejected)) <= 1e-9 else 0,
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
                       (set_vals["completed_qty"], set_vals["process_loss_qty"], set_vals["op_reported"], set_vals["op_reported_by_user"], set_vals["op_reported_dt"], wo.name, op_row.get("idx") or (idx+1)))
    except Exception:
        nts.log_error(nts.get_traceback(), "work order operation update failed")

    # ---------------- Update Work Order produced_qty from LAST operation (not sum) ----------
    try:
        rows = nts.db.sql("SELECT completed_qty FROM `tabWork Order Operation` WHERE parent=%s ORDER BY idx DESC LIMIT 1", (wo.name,))
        last_completed = flt(rows[0][0] or 0) if rows else 0.0
        wo_qty = flt(wo.get("qty") or wo.get("production_qty") or 0)

        produced_to_set = last_completed
        if wo_qty and produced_to_set > wo_qty:
            produced_to_set = wo_qty
        if produced_to_set < 0:
            produced_to_set = 0.0

        try:
            nts.db.set_value("Work Order", wo.name, {"produced_qty": produced_to_set}, update_modified=False)
        except Exception:
            try:
                nts.db.sql("UPDATE `tabWork Order` SET produced_qty=%s WHERE name=%s", (produced_to_set, wo.name))
            except Exception:
                nts.log_error(nts.get_traceback(), "Unable to set Work Order.produced_qty")

        # set in-progress safely (clamped >= 0) into a typical candidate field
        in_progress_val = 0.0
        try:
            in_progress_val = max(0.0, (wo_qty - produced_to_set) if wo_qty else 0.0)
        except Exception:
            in_progress_val = 0.0

        try:
            candidate_fields = ["in_progress_qty", "items_in_progress", "qty_in_progress", "in_progress", "in_progress_count"]
            updated = False
            for fld in candidate_fields:
                cols = nts.db.sql("SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='tabWork Order' AND COLUMN_NAME=%s", (fld,))
                if cols:
                    try:
                        nts.db.set_value("Work Order", wo.name, {fld: in_progress_val}, update_modified=False)
                        updated = True
                        break
                    except Exception:
                        try:
                            nts.db.sql(f"UPDATE `tabWork Order` SET `{fld}`=%s WHERE name=%s", (in_progress_val, wo.name))
                            updated = True
                            break
                        except Exception:
                            nts.log_error(nts.get_traceback(), "Unable to set Work Order.%s" % fld)
            if not updated:
                try:
                    cols2 = nts.db.sql("SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='tabWork Order' AND COLUMN_NAME='r_in_progress'")
                    if cols2:
                        nts.db.set_value("Work Order", wo.name, {"r_in_progress": in_progress_val}, update_modified=False)
                except Exception:
                    pass
        except Exception:
            nts.log_error(nts.get_traceback(), "in_progress update failed")
    except Exception:
        nts.log_error(nts.get_traceback(), "final produced_qty/in_progress calculation failed")

    # ---------------- per-punch audit log (non-critical) ----------------
    try:
        _ensure_punch_table()
        row_name = _make_name("OPLOG")
        nts.db.sql("""INSERT INTO `tabOperation Punch Log` (name, creation, modified, owner, parent_work_order, parent_op_idx, parent_op_name, employee_number, employee_name, produced_qty, rejected_qty, posting_datetime)
                      VALUES (%s, NOW(), NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                   (row_name, nts.session.user, wo.name, idx, op_text, emp_display_number, emp_display_name, produced_qty, process_loss, posting_dt))
    except Exception:
        nts.log_error(nts.get_traceback(), "punch log insert failed (non-fatal)")

    remaining = max(0.0, available_input - (final_completed + final_rejected))
    remaining = flt(remaining)
    return _("Operation {0} reported: produced {1}, rejected {2}. Remaining for this op: {3}").format(op_text, produced_qty, process_loss, remaining)
