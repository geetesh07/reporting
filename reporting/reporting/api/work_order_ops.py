# apps/reporting/reporting/api/work_order_ops.py
# Full server implementation — robust job card submit + punch logs + exports.
import nts
from nts import _
from nts.utils import flt, get_datetime, now_datetime
import uuid
import traceback
import io, csv

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
        try:
            nts.log_error(nts.get_traceback(), "ensure_punch_table failed")
        except Exception:
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
    """Return allowed tokens for a workstation safely."""
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
    """Return punch logs grouped by op index for UI rendering."""
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
def export_punch_report_csv(work_order):
    """Return CSV string of punch logs for reporting/accountability."""
    _ensure_punch_table()
    try:
        rows = nts.db.sql("""
            SELECT parent_op_idx AS op_idx, parent_op_name AS operation, employee_number, employee_name, produced_qty, rejected_qty, posting_datetime
            FROM `tabOperation Punch Log`
            WHERE parent_work_order=%s
            ORDER BY parent_op_idx ASC, posting_datetime ASC
        """, (work_order,), as_dict=True)
    except Exception:
        rows = []
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["op_idx", "operation", "employee_number", "employee_name", "produced_qty", "rejected_qty", "posting_datetime"])
    for r in rows:
        writer.writerow([
            r.get("op_idx"),
            r.get("operation"),
            r.get("employee_number"),
            r.get("employee_name"),
            float(r.get("produced_qty") or 0),
            float(r.get("rejected_qty") or 0),
            str(r.get("posting_datetime") or "")
        ])
    return output.getvalue()

@nts.whitelist()
def report_operation(work_order, op_index, operation_name, employee_number, produced_qty, process_loss=0, posting_datetime=None, complete_operation=1):
    """
    Report produced/rejected for a Work Order operation.
    - Strict quantity validation
    - Partial punches appended to an unsubmitted Job Card
    - JC will be submitted only when complete_operation==1 (using framework submit)
    - If JC submit raises, the full traceback is thrown to UI (so you can see validation/permission error)
    """
    produced_qty = flt(produced_qty or 0)
    process_loss = flt(process_loss or 0)
    if produced_qty <= 0 and process_loss <= 0:
        nts.throw(_("Either produced qty or rejected qty must be greater than zero."))

    posting_dt = get_datetime(posting_datetime) if posting_datetime else now_datetime()

    # employee lookup (by employee_number)
    emp_docname = None
    emp_label = ""
    try:
        emp = nts.db.get_value("Employee", {"employee_number": employee_number}, ["name", "employee_name"], as_dict=True)
        if not emp or not emp.get("name"):
            nts.throw(_("Employee {0} not found.").format(employee_number))
        emp_docname = emp.get("name")
        emp_label = str(emp.get("employee_name") or "")
    except Exception:
        nts.throw(_("Employee {0} not found.").format(employee_number))

    # load WO
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

    # enforce next-pending operation only
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

    # compute available_input = min(required_qty, previous_op_completed)
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

    # read current op totals from DB authoritative
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

    # validations for input qtys
    if (produced_qty + process_loss) - 1e-9 > pending_qty:
        nts.throw(_("Produced + Rejected ({0}) exceeds pending ({1}).").format(produced_qty + process_loss, pending_qty))
    if int(complete_operation or 0) == 1 and abs((produced_qty + process_loss) - pending_qty) > 1e-6:
        nts.throw(_("To complete, produced + rejected must equal pending ({0}).").format(pending_qty))

    op_text = op_row.get("operation") or op_row.get("operation_name") or operation_name or ""

    # find existing job card candidate (prefer unsubmitted)
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
        if not jc_name and jc_candidates:
            jc_name = jc_candidates[0][0]

    jc_doc = None
    if jc_name:
        try:
            jc_doc = nts.get_doc("Job Card", jc_name)
        except Exception:
            jc_doc = None

    workstation_val = op_row.get("workstation") or wo.get("workstation") or ""
    jobcard_was_submitted = False

    def compute_minutes(from_time, to_time):
        try:
            fd = get_datetime(from_time)
            td = get_datetime(to_time)
            diff = (td - fd).total_seconds()
            return max(0, int(round(diff / 60.0)))
        except Exception:
            return 0

    def append_and_submit_jc(jc, emp_docname_local, submit_on_complete=False):
        """
        Append a time_log to jc and submit using framework APIs.
        If submit fails, raise nts.throw with full traceback so UI shows the error.
        Returns True if JC submitted.
        """
        try:
            # make sure required basics exist
            if not getattr(jc, "work_order", None):
                jc.work_order = wo.name
            if not getattr(jc, "operation", None):
                jc.operation = op_text
            if not getattr(jc, "for_quantity", None):
                jc.for_quantity = required_qty
            if workstation_val and not getattr(jc, "workstation", None):
                jc.workstation = workstation_val

            from_time_cand = pick_from_time(idx, wo)
            to_time_val = posting_dt
            minutes = compute_minutes(from_time_cand, to_time_val)

            tl = {
                "employee": emp_docname_local,
                "from_time": from_time_cand,
                "to_time": to_time_val,
                "time_in_mins": minutes,
                "completed_qty": produced_qty
            }
            try:
                tl["rejected_qty"] = process_loss
            except Exception:
                pass

            jc.append("time_logs", tl)
            # save via framework API
            jc.flags = getattr(jc, "flags", {}) or {}
            jc.flags.ignore_permissions = True
            try:
                jc.save(ignore_permissions=True)
            except Exception as exc:
                tb = traceback.format_exc()
                nts.log_error(tb, "Job Card save failed")
                nts.throw(_("Job Card save failed: {0}").format(str(exc) + "\n\n" + tb))

            # submit using framework, making errors visible
            if submit_on_complete:
                try:
                    if getattr(jc, "docstatus", 0) == 0:
                        # ensure required fields present again right before submit
                        if not getattr(jc, "workstation", None) and workstation_val:
                            jc.workstation = workstation_val
                        if not getattr(jc, "for_quantity", None):
                            jc.for_quantity = required_qty
                        jc.submit()
                        try:
                            nts.db.commit()
                        except Exception:
                            pass
                        return True
                except Exception as exc:
                    tb = traceback.format_exc()
                    nts.log_error(tb, "Job Card submit failed")
                    # raise visible error so you can see exact validation/permission problem
                    nts.throw(_("Job Card submit failed: {0}").format(str(exc) + "\n\n" + tb))
            return False
        except Exception:
            # re-raise to be handled by caller (we purposely do not swallow)
            raise

    try:
        # Create or reuse JC and append time_log (and attempt submit if requested)
        if jc_doc:
            if getattr(jc_doc, "docstatus", 0) == 0:
                jobcard_was_submitted = append_and_submit_jc(jc_doc, emp_docname, submit_on_complete=(int(complete_operation or 0) == 1))
                jc_name = jc_doc.name
            else:
                # most recent JC is submitted, create a new one if workstation available
                if workstation_val:
                    try:
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
                            jobcard_was_submitted = append_and_submit_jc(new_jc, emp_docname, submit_on_complete=(int(complete_operation or 0) == 1))
                            jc_name = new_jc.name
                    except Exception as exc:
                        tb = traceback.format_exc()
                        nts.log_error(tb, "New JC creation failed")
                        nts.throw(_("Job Card creation failed: {0}").format(tb))
                else:
                    # no workstation — skip JC creation; totals will be updated below
                    pass
        else:
            if workstation_val:
                try:
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
                        jobcard_was_submitted = append_and_submit_jc(new_jc, emp_docname, submit_on_complete=(int(complete_operation or 0) == 1))
                        jc_name = new_jc.name
                except Exception as exc:
                    tb = traceback.format_exc()
                    nts.log_error(tb, "Initial JC creation failed")
                    nts.throw(_("Job Card creation failed: {0}").format(tb))
            else:
                # skip JC creation
                pass
    except nts.NtsException:
        # rethrow visible exceptions (we intentionally used nts.throw earlier)
        raise
    except Exception:
        # unexpected bubbled exception
        tb = traceback.format_exc()
        nts.log_error(tb, "job card handling failed (unexpected)")
        nts.throw(_("Unexpected job card error: {0}").format(tb))

    # Update operation totals in Work Order Operation
    final_completed = None
    final_rejected = None
    if jobcard_was_submitted:
        # read authoritative totals from DB (job card submit should have updated via hooks)
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

    # If we did not rely on JC submit, add the produced/rejected to current totals
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
            rep = emp_label or employee_number or ""
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

    # Update Work Order.produced_qty from last operation (clamped)
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
    except Exception:
        nts.log_error(nts.get_traceback(), "final produced_qty calculation failed")

    # Write punch audit row (best effort)
    try:
        _ensure_punch_table()
        row_name = _make_name("OPLOG")
        nts.db.sql("""INSERT INTO `tabOperation Punch Log` (name, creation, modified, owner, parent_work_order, parent_op_idx, parent_op_name, employee_number, employee_name, produced_qty, rejected_qty, posting_datetime)
                      VALUES (%s, NOW(), NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                   (row_name, nts.session.user, wo.name, idx, op_text, employee_number, emp_label, produced_qty, process_loss, posting_dt))
    except Exception:
        nts.log_error(nts.get_traceback(), "punch log insert failed (non-fatal)")

    # Now: re-load Work Order via framework and save it so ERPNext/your app hooks set status/finish button behavior properly.
    try:
        wo_doc = nts.get_doc("Work Order", wo.name)
        wo_doc.flags = getattr(wo_doc, "flags", {}) or {}
        wo_doc.flags.ignore_permissions = True
        try:
            wo_doc.save(ignore_permissions=True)
            try:
                nts.db.commit()
            except Exception:
                pass
        except Exception as exc:
            tb = traceback.format_exc()
            nts.log_error(tb, "Work Order save after reporting failed")
            nts.throw(_("Work Order post-update failed: {0}").format(str(exc) + "\n\n" + tb))
    except Exception:
        nts.log_error(nts.get_traceback(), "Work Order reload after reporting failed")

    # compute remaining for response
    try:
        if op_row.get("name"):
            vals = nts.db.get_value("Work Order Operation", op_row.get("name"), ["completed_qty", "process_loss_qty"], as_dict=True)
            if vals:
                a_completed = flt(vals.get("completed_qty") or 0)
                a_rejected = flt(vals.get("process_loss_qty") or 0)
            else:
                a_completed = final_completed
                a_rejected = final_rejected
        else:
            rows = nts.db.sql("""SELECT completed_qty, process_loss_qty FROM `tabWork Order Operation` WHERE parent=%s AND idx=%s LIMIT 1""", (wo.name, op_row.get("idx") or (idx+1)))
            if rows and rows[0]:
                a_completed = flt(rows[0][0] or 0)
                a_rejected = flt(rows[0][1] or 0)
            else:
                a_completed = final_completed
                a_rejected = final_rejected
        remaining = max(0.0, available_input - (a_completed + a_rejected))
        remaining = flt(remaining)
    except Exception:
        remaining = 0.0

    return _("Operation {0} reported: produced {1}, rejected {2}. Remaining for this op: {3}").format(op_text, produced_qty, process_loss, remaining)
