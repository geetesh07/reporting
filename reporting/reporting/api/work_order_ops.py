import nts
from nts import _
from nts.utils import flt, get_datetime, now_datetime

@nts.whitelist()
def report_operation(work_order, op_index, operation_name, employee_number, produced_qty, process_loss=0, posting_datetime=None, complete_operation=1):
    produced_qty = flt(produced_qty or 0)
    process_loss = flt(process_loss or 0)
    if produced_qty <= 0 and process_loss <= 0:
        nts.throw(_("Either produced qty or rejected qty must be greater than zero."))
    posting_dt = get_datetime(posting_datetime) if posting_datetime else now_datetime()
    emp = nts.db.get_value("Employee", {"employee_number": employee_number}, ["name", "employee_name", "employee_number"], as_dict=True)
    if not emp or not emp.get("name"):
        nts.throw(_("Employee {0} not found.").format(employee_number))
    emp_docname = emp.get("name")
    emp_label = str(emp.get("employee_name") or "").strip()
    emp_number = str(emp.get("employee_number") or "").strip()
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
    existing_completed = flt(op_row.get("completed_qty") or 0)
    existing_loss = flt(op_row.get("process_loss_qty") or 0)
    pending_qty = required_qty - (existing_completed + existing_loss)
    if (produced_qty + process_loss) - 1e-9 > pending_qty:
        nts.throw(_("Produced + Rejected ({0}) exceeds pending ({1}).").format(produced_qty + process_loss, pending_qty))
    if int(complete_operation or 0) == 1 and abs((produced_qty + process_loss) - pending_qty) > 1e-6:
        nts.throw(_("To complete, produced + rejected must equal pending ({0}).").format(pending_qty))
    workstation = op_row.get("workstation") or None
    if workstation:
        allowed_csv = ""
        for fld in ("authorized_employee_numbers", "authorized_employee_ids"):
            try:
                val = nts.db.get_value("Workstation", workstation, fld)
                if val:
                    allowed_csv = val
                    break
            except Exception:
                pass
        allowed_list = []
        if allowed_csv and str(allowed_csv).strip():
            for tok in str(allowed_csv).replace(";", ",").split(","):
                tok = tok.strip()
                if tok:
                    allowed_list.append(tok.lower())
        if allowed_list:
            matched = any(t == emp_number.lower() or t == emp_docname.lower() or t == emp_label.lower() for t in allowed_list)
            if not matched:
                nts.throw(_("You are not authorized to report on workstation {0}.").format(workstation))
    def find_job_card(wo_name, op_text):
        rows = nts.db.sql("select name from `tabJob Card` where work_order=%s and operation=%s order by creation desc limit 1", (wo_name, op_text))
        return rows[0][0] if rows else None
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
            return get_datetime(prev.get("op_reported_dt"))
        prev_op_text = prev.get("operation") or prev.get("operation_name") or ""
        jc_prev = find_job_card(wo_doc.name, prev_op_text)
        if jc_prev:
            try:
                jc_prev_doc = nts.get_doc("Job Card", jc_prev)
                if getattr(jc_prev_doc, "to_time", None):
                    return get_datetime(jc_prev_doc.to_time) if jc_prev_doc.to_time else jc_prev_doc.to_time
            except Exception:
                pass
        return now_datetime()
    op_text = op_row.get("operation") or op_row.get("operation_name") or operation_name or ""
    jc_name = find_job_card(wo.name, op_text)
    jc_doc = nts.get_doc("Job Card", jc_name) if jc_name else None
    if not jc_doc:
        orig_msgprint = getattr(nts, "msgprint", None)
        try:
            nts.msgprint = lambda *a, **k: None
            jc_doc = nts.get_doc({"doctype": "Job Card", "work_order": wo.name, "operation": op_text, "for_quantity": required_qty})
            jc_doc.insert(ignore_permissions=True)
        finally:
            if orig_msgprint is not None:
                nts.msgprint = orig_msgprint
    if getattr(jc_doc, "docstatus", 0) == 1:
        nts.throw(_("Job Card {0} already submitted.").format(jc_doc.name))
    from_time_candidate = pick_from_time(idx, wo)
    to_time = posting_dt
    try:
        minutes = max(0, int(round((get_datetime(str(to_time)) - get_datetime(str(from_time_candidate))).total_seconds() / 60.0)))
    except Exception:
        minutes = 0
    has_rejected_col = False
    try:
        rows = nts.db.sql("""SELECT COLUMN_NAME FROM information_schema.COLUMNS
                             WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'tabJob Card Time Log' AND COLUMN_NAME = 'rejected_qty'""")
        has_rejected_col = bool(rows)
    except Exception:
        has_rejected_col = False
    from_str = str(from_time_candidate)
    to_str = str(to_time)
    existing_rows = None
    try:
        if has_rejected_col:
            existing_rows = nts.db.sql("select name, completed_qty, rejected_qty from `tabJob Card Time Log` where parent=%s and employee=%s and from_time=%s and to_time=%s order by creation asc", (jc_doc.name, emp_docname, from_str, to_str))
        else:
            existing_rows = nts.db.sql("select name, completed_qty from `tabJob Card Time Log` where parent=%s and employee=%s and from_time=%s and to_time=%s order by creation asc", (jc_doc.name, emp_docname, from_str, to_str))
    except Exception:
        existing_rows = None
    if existing_rows and len(existing_rows) > 0:
        keeper = existing_rows[0][0]
        prev_completed = flt(existing_rows[0][1] or 0)
        prev_rejected = flt(existing_rows[0][2] or 0) if has_rejected_col and len(existing_rows[0]) > 2 else 0
        total_completed = prev_completed + produced_qty
        total_rejected = prev_rejected + process_loss
        try:
            if has_rejected_col:
                nts.db.set_value("Job Card Time Log", keeper, {"time_in_mins": minutes, "completed_qty": total_completed, "rejected_qty": total_rejected}, update_modified=False)
            else:
                nts.db.set_value("Job Card Time Log", keeper, {"time_in_mins": minutes, "completed_qty": total_completed}, update_modified=False)
        except Exception:
            pass
        if len(existing_rows) > 1:
            dup_names = [r[0] for r in existing_rows[1:]]
            for dn in dup_names:
                try:
                    nts.db.sql("delete from `tabJob Card Time Log` where name=%s", (dn,))
                except Exception:
                    try:
                        nts.db.set_value("Job Card Time Log", dn, {"deleted": 1}, update_modified=False)
                    except Exception:
                        pass
    else:
        other_rows = None
        try:
            other_rows = nts.db.sql("select name from `tabJob Card Time Log` where parent=%s and employee=%s order by creation asc", (jc_doc.name, emp_docname))
        except Exception:
            other_rows = None
        if other_rows and len(other_rows) > 0:
            keeper = other_rows[0][0]
            try:
                if has_rejected_col:
                    nts.db.set_value("Job Card Time Log", keeper, {"time_in_mins": minutes, "completed_qty": flt(produced_qty), "rejected_qty": flt(process_loss)}, update_modified=False)
                else:
                    nts.db.set_value("Job Card Time Log", keeper, {"time_in_mins": minutes, "completed_qty": flt(produced_qty)}, update_modified=False)
            except Exception:
                pass
            if len(other_rows) > 1:
                dup_names = [r[0] for r in other_rows[1:]]
                for dn in dup_names:
                    try:
                        nts.db.sql("delete from `tabJob Card Time Log` where name=%s", (dn,))
                    except Exception:
                        try:
                            nts.db.set_value("Job Card Time Log", dn, {"deleted": 1}, update_modified=False)
                        except Exception:
                            pass
        else:
            try:
                jc_doc.append("time_logs", {
                    "employee": emp_docname,
                    "from_time": from_time_candidate,
                    "to_time": to_time,
                    "time_in_mins": minutes,
                    "completed_qty": produced_qty,
                    "rejected_qty": process_loss
                } if has_rejected_col else {
                    "employee": emp_docname,
                    "from_time": from_time_candidate,
                    "to_time": to_time,
                    "time_in_mins": minutes,
                    "completed_qty": produced_qty
                })
            except Exception:
                try:
                    tl = nts.get_doc({
                        "doctype": "Job Card Time Log",
                        "parent": jc_doc.name,
                        "parentfield": "time_logs",
                        "parenttype": "Job Card",
                        "employee": emp_docname,
                        "from_time": from_time_candidate,
                        "to_time": to_time,
                        "time_in_mins": minutes,
                        "completed_qty": produced_qty
                    })
                    if has_rejected_col:
                        try:
                            tl.rejected_qty = process_loss
                        except Exception:
                            pass
                    tl.insert(ignore_permissions=True)
                except Exception:
                    pass
    orig_msgprint = getattr(nts, "msgprint", None)
    try:
        nts.msgprint = lambda *a, **k: None
        try:
            jc_doc.save(ignore_permissions=True)
        except Exception:
            nts.log_error(nts.get_traceback(), "job card save failed")
        try:
            if getattr(jc_doc, "docstatus", 0) == 0:
                jc_doc.submit()
        except Exception as e:
            nts.log_error(nts.get_traceback(), "job card submit failed")
            raise
    finally:
        if orig_msgprint is not None:
            nts.msgprint = orig_msgprint
    if op_row.get("name"):
        set_vals = {
            "op_reported": 1 if int(complete_operation or 0) == 1 else 0,
            "op_reported_by_employee": emp_docname,
            "op_reported_by_user": nts.session.user,
            "op_reported_dt": str(to_time)
        }
        try:
            cols = nts.db.sql("SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='tabWork Order Operation' AND COLUMN_NAME='op_reported_by_employee_name'")
            if cols:
                set_vals["op_reported_by_employee_name"] = emp_label
        except Exception:
            pass
        try:
            nts.db.set_value("Work Order Operation", op_row.get("name"), set_vals, update_modified=False)
        except Exception:
            pass
    return _("Operation {0} reported: produced {1}, rejected {2}").format(op_text, produced_qty, process_loss)
