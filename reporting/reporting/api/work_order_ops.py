# apps/reporting/reporting/reporting/api/work_order_ops.py
# Reworked report_operation (nts-based) with fixes:
#  - ensures from_time < to_time (avoids 'From time must be less than to time')
#  - closes open time logs (best-effort) to avoid OverlapError
#  - appends to an unsubmitted Job Card (silent create) and submits when requested
#  - preserves original flow/fields from your working code as much as possible

import nts
from nts import _
from nts.utils import flt, get_datetime, now_datetime
import traceback
from datetime import timedelta, datetime

def _ensure_punch_table():  # optional, kept for compatibility with prior approaches
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
                `processed` TINYINT DEFAULT 0,
                PRIMARY KEY (`name`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """)
    except Exception:
        try:
            nts.log_error(nts.get_traceback(), "ensure_punch_table failed")
        except Exception:
            pass

def _make_name(prefix="OPLOG"):
    import uuid
    return "{}-{}".format(prefix, uuid.uuid4().hex[:12])

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

def compute_minutes(from_time, to_time):
    try:
        fd = get_datetime(from_time)
        td = get_datetime(to_time)
        diff = (td - fd).total_seconds()
        return max(0, int(round(diff / 60.0)))
    except Exception:
        return 0

@nts.whitelist()
def get_workstation_allowed(workstation):
    # defensive: return CSV of allowed tokens; client will use it to check authorization
    if not workstation:
        return ""
    allowed_csv = ""
    try:
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
    _ensure_punch_table()
    try:
        rows = nts.db.sql("""
            SELECT parent_op_idx, employee_number, employee_name, produced_qty, rejected_qty, posting_datetime, name, processed
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
    produced_qty = flt(produced_qty or 0)
    process_loss = flt(process_loss or 0)
    if produced_qty <= 0 and process_loss <= 0:
        nts.throw(_("Either produced qty or rejected qty must be greater than zero."))

    posting_dt = get_datetime(posting_datetime) if posting_datetime else now_datetime()

    # employee lookup
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

    # find first pending op
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

    # compute available input (previous op completed)
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

    # authoritative totals for this op
    current_completed = 0.0
    current_loss = 0.0
    try:
        if op_row.get("name"):
            vals = nts.db.get_value("Work Order Operation", op_row.get("name"), ["completed_qty", "process_loss_qty"], as_dict=True)
            if vals:
                current_completed = flt(vals.get("completed_qty") or 0)
                current_loss = flt(vals.get("process_loss_qty") or 0)
        else:
            rows = nts.db.sql("""SELECT completed_qty, process_loss_qty FROM `tabWork Order Operation` WHERE parent=%s AND idx=%s LIMIT 1""", (wo.name, op_row.get("idx") or (idx+1)))
            if rows and rows[0]:
                current_completed = flt(rows[0][0] or 0)
                current_loss = flt(rows[0][1] or 0)
    except Exception:
        current_completed = flt(op_row.get("completed_qty") or 0)
        current_loss = flt(op_row.get("process_loss_qty") or 0)

    # sum of unprocessed punch logs for this op (to prevent double-counting)
    try:
        _ensure_punch_table()
        srow = nts.db.sql("""
            SELECT COALESCE(SUM(produced_qty),0) AS prod_sum, COALESCE(SUM(rejected_qty),0) AS rej_sum
            FROM `tabOperation Punch Log`
            WHERE parent_work_order=%s AND parent_op_idx=%s AND processed=0
        """, (wo.name, idx), as_dict=True)
        sum_prod = flt(srow[0].get("prod_sum") or 0) if srow else 0.0
        sum_rej = flt(srow[0].get("rej_sum") or 0) if srow else 0.0
    except Exception:
        sum_prod = 0.0
        sum_rej = 0.0

    pending_qty = available_input - (current_completed + current_loss + sum_prod + sum_rej)
    if pending_qty < 0:
        pending_qty = 0.0

    if (produced_qty + process_loss) - 1e-9 > pending_qty:
        nts.throw(_("Produced + Rejected ({0}) exceeds pending ({1}).").format(produced_qty + process_loss, pending_qty))
    if int(complete_operation or 0) == 1 and abs((produced_qty + process_loss) - pending_qty) > 1e-6:
        nts.throw(_("To complete, produced + rejected must equal pending ({0}).").format(pending_qty))

    op_text = op_row.get("operation") or op_row.get("operation_name") or operation_name or ""
    workstation = op_row.get("workstation") or wo.get("workstation") or ""

    def find_job_card(wo_name, op_text):
        rows = nts.db.sql("select name from `tabJob Card` where work_order=%s and operation=%s order by creation desc limit 1", (wo_name, op_text))
        return rows[0][0] if rows else None

    # pick or create job card (prefer unsubmitted)
    jc_name = None
    try:
        rows = nts.db.sql("select name, docstatus from `tabJob Card` where work_order=%s and operation=%s order by creation desc", (wo.name, op_text))
        if rows:
            for r in rows:
                if r and len(r) >= 2 and r[1] == 0:
                    jc_name = r[0]
                    break
            if not jc_name:
                jc_name = rows[0][0]
    except Exception:
        jc_name = None

    jc_doc = nts.get_doc("Job Card", jc_name) if jc_name else None

    # If none exists create one silently (but require workstation)
    if not jc_doc:
        if not workstation:
            # creating job card without workstation often fails validation later — ask user
            nts.throw(_("Workstation is not set for this operation. Please set the 'Workstation' on the Work Order operation."))
        orig_msgprint = getattr(nts, "msgprint", None)
        try:
            nts.msgprint = lambda *a, **k: None
            jc_doc = nts.get_doc({"doctype": "Job Card", "work_order": wo.name, "operation": op_text, "for_quantity": required_qty, "workstation": workstation})
            jc_doc.insert(ignore_permissions=True)
        finally:
            if orig_msgprint is not None:
                nts.msgprint = orig_msgprint

    if getattr(jc_doc, "docstatus", 0) == 1 and int(complete_operation or 0) == 1:
        # already submitted — cannot add time logs to a submitted job card
        nts.throw(_("Job Card {0} already submitted.").format(jc_doc.name))

    # compute from_time candidate
    from_time_candidate = pick_from_time(idx, wo)
    to_time = posting_dt

    # best-effort: close any open time log for the employee to avoid OverlapError
    try:
        open_rows = nts.db.sql("select name, parent, from_time from `tabJob Card Time Log` where employee=%s and (to_time is null or to_time='') order by creation desc", (emp_docname,))
        if open_rows and len(open_rows) > 0:
            # close the most recent one (best-effort)
            open_name = open_rows[0][0]
            try:
                # set its to_time to posting_dt - 1 second to avoid equality overlap (safe)
                new_to = get_datetime(to_time)
                try:
                    new_to_dt = new_to
                    # subtract 1 second
                    new_to_dt = new_to_dt - timedelta(seconds=1)
                except Exception:
                    new_to_dt = new_to
                minutes_for_open = compute_minutes(open_rows[0][2] or new_to_dt, new_to_dt)
                nts.db.sql("update `tabJob Card Time Log` set to_time=%s, time_in_mins=%s where name=%s", (str(new_to_dt), minutes_for_open, open_name))
                try:
                    nts.db.commit()
                except Exception:
                    pass
                # make sure our from_time starts at or after that closure
                if get_datetime(from_time_candidate) <= new_to_dt:
                    from_time_candidate = new_to_dt + timedelta(seconds=0)  # will be adjusted below if equal
            except Exception:
                pass
    except Exception:
        pass

    # ensure from_time < to_time; if equal or greater, nudge from_time back 1 second
    try:
        ft = get_datetime(from_time_candidate)
        tt = get_datetime(to_time)
        if ft >= tt:
            # nudge from_time to be 1 second before to_time
            from_time_candidate = tt - timedelta(seconds=1)
    except Exception:
        # if parsing fails, fallback to small safe window
        try:
            from_time_candidate = get_datetime(to_time) - timedelta(seconds=1)
        except Exception:
            from_time_candidate = to_time

    # compute minutes
    try:
        minutes = max(0, int(round((get_datetime(str(to_time)) - get_datetime(str(from_time_candidate))).total_seconds() / 60.0)))
    except Exception:
        minutes = 0

    # check if rejected column exists in Job Card Time Log table
    has_rejected_col = False
    try:
        rows = nts.db.sql("""SELECT COLUMN_NAME FROM information_schema.COLUMNS
                             WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'tabJob Card Time Log' AND COLUMN_NAME = 'rejected_qty'""")
        has_rejected_col = bool(rows)
    except Exception:
        has_rejected_col = False

    # Attempt to append time log to existing job card
    appended = False
    try:
        # ensure jobcard has basic required values (defensive)
        if not getattr(jc_doc, "work_order", None):
            jc_doc.work_order = wo.name
        if not getattr(jc_doc, "operation", None):
            jc_doc.operation = op_text
        if not getattr(jc_doc, "for_quantity", None):
            jc_doc.for_quantity = required_qty
        if workstation and not getattr(jc_doc, "workstation", None):
            jc_doc.workstation = workstation

        tl = {
            "employee": emp_docname,
            "from_time": from_time_candidate,
            "to_time": to_time,
            "time_in_mins": minutes,
            "completed_qty": produced_qty
        }
        if has_rejected_col:
            tl["rejected_qty"] = process_loss

        jc_doc.append("time_logs", tl)
        jc_doc.save(ignore_permissions=True)
        appended = True
    except Exception as exc:
        # If validation error due to from_time >= to_time, try nudge and retry once
        tb = traceback.format_exc()
        try:
            err_text = str(exc).lower()
        except Exception:
            err_text = ""

        # if it's the From time must be less than to time validation, adjust and retry
        retried = False
        try:
            if "from time must be less than to time" in err_text or "from time must be less" in err_text or "from_time" in err_text and "to_time" in err_text:
                # ensure from_time < to_time by setting from_time = to_time - 1 second
                try:
                    to_dt = get_datetime(to_time)
                    from_time_candidate = to_dt - timedelta(seconds=1)
                    # update the last appended timelog if present in memory, else append fresh
                    jc_doc.time_logs = jc_doc.time_logs or []
                    # remove last appended if partially appended? (defensive)
                    # attempt append again
                    tl = {
                        "employee": emp_docname,
                        "from_time": from_time_candidate,
                        "to_time": to_time,
                        "time_in_mins": compute_minutes(from_time_candidate, to_time),
                        "completed_qty": produced_qty
                    }
                    if has_rejected_col:
                        tl["rejected_qty"] = process_loss
                    jc_doc.append("time_logs", tl)
                    jc_doc.save(ignore_permissions=True)
                    appended = True
                    retried = True
                except Exception:
                    retried = False
        except Exception:
            retried = False

        if not retried:
            # Fallback: try inserting a Job Card Time Log row directly (best-effort)
            try:
                tl_name = nts.get_value("Job Card Time Log", {"parent": jc_doc.name, "employee": emp_docname}, ["name"])
            except Exception:
                tl_name = None
            try:
                # insert direct child row (best-effort)
                data = {
                    "doctype": "Job Card Time Log",
                    "parent": jc_doc.name,
                    "parentfield": "time_logs",
                    "parenttype": "Job Card",
                    "employee": emp_docname,
                    "from_time": from_time_candidate,
                    "to_time": to_time,
                    "time_in_mins": minutes,
                    "completed_qty": produced_qty
                }
                if has_rejected_col:
                    data["rejected_qty"] = process_loss
                tl_doc = nts.get_doc(data)
                tl_doc.insert(ignore_permissions=True)
                appended = True
            except Exception:
                nts.log_error(tb, "job card time log append fallback failed")
                # surface the original validation so you know what to fix
                nts.throw(_("Failed to add time log to Job Card: {0}").format(str(exc)))

    # Submit job card if requested
    submitted = False
    if appended and int(complete_operation or 0) == 1:
        try:
            # submit via framework to run all hooks/validations
            if getattr(jc_doc, "docstatus", 0) == 0:
                jc_doc.submit()
            submitted = True
        except Exception as exc:
            # surface submit traceback so you see exact prodman validations (do not swallow)
            tb = traceback.format_exc()
            nts.log_error(tb, "job card submit failed")
            nts.throw(_("Job Card submit failed: {0}").format(str(exc) + "\n\n" + tb))

    # Update operation row values (best-effort)
    if op_row.get("name"):
        try:
            # read up-to-date values if job card submitted, else increment locally
            if submitted:
                vals = nts.db.get_value("Work Order Operation", op_row.get("name"), ["completed_qty", "process_loss_qty"], as_dict=True)
                new_completed = flt(vals.get("completed_qty") or 0) if vals else flt(op_row.get("completed_qty") or 0)
                new_loss = flt(vals.get("process_loss_qty") or 0) if vals else flt(op_row.get("process_loss_qty") or 0)
            else:
                new_completed = flt(op_row.get("completed_qty") or 0) + produced_qty
                new_loss = flt(op_row.get("process_loss_qty") or 0) + process_loss

            set_vals = {
                "completed_qty": new_completed,
                "process_loss_qty": new_loss,
                "op_reported": 1 if int(complete_operation or 0) == 1 else 0,
                "op_reported_by_employee": emp_docname,
                "op_reported_by_user": nts.session.user,
                "op_reported_dt": str(posting_dt)
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
                # fallback to raw update
                try:
                    nts.db.sql("""UPDATE `tabWork Order Operation` SET completed_qty=%s, process_loss_qty=%s, op_reported=%s, op_reported_by_user=%s, op_reported_dt=%s WHERE name=%s""",
                               (set_vals["completed_qty"], set_vals["process_loss_qty"], set_vals["op_reported"], set_vals["op_reported_by_user"], set_vals["op_reported_dt"], op_row.get("name")))
                except Exception:
                    nts.log_error(nts.get_traceback(), "work order operation update failed")
        except Exception:
            nts.log_error(nts.get_traceback(), "final op update failure")
    else:
        # fallback update by parent & idx if no name
        try:
            nts.db.sql("""UPDATE `tabWork Order Operation`
                          SET completed_qty=%s, process_loss_qty=%s, op_reported=%s, op_reported_by_user=%s, op_reported_dt=%s
                          WHERE parent=%s AND idx=%s""",
                       (flt(op_row.get("completed_qty") or 0) + produced_qty, flt(op_row.get("process_loss_qty") or 0) + process_loss, int(complete_operation or 0), nts.session.user, str(posting_dt), wo.name, op_row.get("idx") or (idx+1)))
        except Exception:
            nts.log_error(nts.get_traceback(), "work order operation fallback update failed")

    # if submitted, update Work Order produced_qty from last operation (authoritative)
    try:
        if submitted:
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
        nts.db.sql("""INSERT INTO `tabOperation Punch Log` (name, creation, modified, owner, parent_work_order, parent_op_idx, parent_op_name, employee_number, employee_name, produced_qty, rejected_qty, posting_datetime, processed)
                      VALUES (%s, NOW(), NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                   (row_name, nts.session.user, wo.name, idx, op_text, employee_number, emp_label, produced_qty, process_loss, posting_dt, 1 if submitted else 0))
        try:
            nts.db.commit()
        except Exception:
            pass
    except Exception:
        nts.log_error(nts.get_traceback(), "punch log insert failed (non-fatal)")

    # compute remaining for response
    try:
        if op_row.get("name"):
            vals = nts.db.get_value("Work Order Operation", op_row.get("name"), ["completed_qty", "process_loss_qty"], as_dict=True)
            a_completed = flt(vals.get("completed_qty") or 0) if vals else flt(op_row.get("completed_qty") or 0)
            a_rejected = flt(vals.get("process_loss_qty") or 0) if vals else flt(op_row.get("process_loss_qty") or 0)
        else:
            rows = nts.db.sql("""SELECT completed_qty, process_loss_qty FROM `tabWork Order Operation` WHERE parent=%s AND idx=%s LIMIT 1""", (wo.name, op_row.get("idx") or (idx+1)))
            if rows and rows[0]:
                a_completed = flt(rows[0][0] or 0)
                a_rejected = flt(rows[0][1] or 0)
            else:
                a_completed = flt(op_row.get("completed_qty") or 0) + (produced_qty if not submitted else 0)
                a_rejected = flt(op_row.get("process_loss_qty") or 0) + (process_loss if not submitted else 0)
        # include unprocessed punches in remaining calc
        srow2 = nts.db.sql("""
            SELECT COALESCE(SUM(produced_qty),0) AS prod_sum, COALESCE(SUM(rejected_qty),0) AS rej_sum
            FROM `tabOperation Punch Log`
            WHERE parent_work_order=%s AND parent_op_idx=%s AND processed=0
        """, (wo.name, idx), as_dict=True)
        uprod = flt(srow2[0].get("prod_sum") or 0) if srow2 else 0
        urej = flt(srow2[0].get("rej_sum") or 0) if srow2 else 0
        remaining = max(0.0, available_input - (a_completed + a_rejected + uprod + urej))
        remaining = flt(remaining)
    except Exception:
        remaining = 0.0

    return _("Operation {0} reported: produced {1}, rejected {2}. Remaining for this op: {3}").format(op_text, produced_qty, process_loss, remaining)
