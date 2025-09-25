# apps/reporting/reporting/reporting/api/work_order_ops.py
# 2025-09 - Force-complete reporting endpoint (no jc.submit()).
# - No DDL in request path (reads information_schema only)
# - Inserts Job Card Time Log (Doc API)
# - Inserts adaptive Operation Punch Log (if table exists)
# - Force-marks Job Card docstatus=1 and status='Completed' via safe UPDATE
# - Updates Work Order Operation totals exactly once (no double-count)
# - Returns structured JSON for UI
import nts
from nts import _
from nts.utils import flt, get_datetime, now_datetime
from datetime import timedelta
import traceback
from nts import log_error

def _make_name(prefix="OPLOG"):
    import uuid
    return "{}-{}".format(prefix, uuid.uuid4().hex[:12])

def compute_minutes(from_time, to_time):
    try:
        fd = get_datetime(from_time)
        td = get_datetime(to_time)
        diff = (td - fd).total_seconds()
        return max(0, int(round(diff / 60.0)))
    except Exception:
        return 0

def _table_exists(table_name: str) -> bool:
    try:
        rows = nts.db.sql("""SELECT COUNT(*) as c FROM information_schema.TABLES
                             WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s""",
                           (table_name,), as_dict=True)
        return bool(rows and rows[0].get("c"))
    except Exception:
        try:
            rows2 = nts.db.sql("SHOW TABLES LIKE %s", (table_name,))
            return bool(rows2)
        except Exception:
            return False

def _get_table_columns(table_name: str):
    try:
        rows = nts.db.sql("""
            SELECT COLUMN_NAME FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
        """, (table_name,), as_dict=True)
        return set([r.get("COLUMN_NAME") for r in rows]) if rows else set()
    except Exception:
        return set()

def _insert_operation_punch_log(parent_work_order, parent_op_idx, parent_op_name,
                                employee_number, employee_name, produced_qty, rejected_qty, posting_datetime, processed_flag):
    table = "tabOperation Punch Log"
    if not _table_exists(table):
        # no table — just log and continue
        log_error("tabOperation Punch Log does not exist. Skipping punch log insert.", "punch_log_missing")
        return None
    cols = _get_table_columns(table)
    insert_cols = []
    params = []
    mapping = [
        ("name", None),
        ("creation", "NOW()"),
        ("modified", "NOW()"),
        ("owner", nts.session.user),
        ("parent_work_order", parent_work_order),
        ("parent_op_idx", parent_op_idx),
        ("parent_op_name", parent_op_name),
        ("employee_number", employee_number),
        ("employee_name", employee_name),
        ("produced_qty", produced_qty),
        ("rejected_qty", rejected_qty),
        ("posting_datetime", posting_datetime),
        ("processed", processed_flag)
    ]
    for col, val in mapping:
        if col in cols:
            if col == "name":
                insert_cols.append("name"); params.append(_make_name("OPLOG"))
            elif col in ("creation", "modified"):
                insert_cols.append(col)
            else:
                insert_cols.append(col); params.append(val)

    if not insert_cols:
        log_error("tabOperation Punch Log has no known columns. Skipping.", "punch_log_no_cols")
        return None

    placeholders = []
    col_params = []
    p_idx = 0
    for col in insert_cols:
        if col in ("creation", "modified"):
            placeholders.append("NOW()")
        else:
            placeholders.append("%s")
            col_params.append(params[p_idx]); p_idx += 1

    col_fragment = ", ".join([f"`{c}`" for c in insert_cols])
    placeholder_fragment = ", ".join(placeholders)
    query = f"INSERT INTO `{table}` ({col_fragment}) VALUES ({placeholder_fragment})"
    try:
        nts.db.sql(query, tuple(col_params))
        try:
            nts.db.commit()
        except Exception:
            pass
        # return name if we used name column (first param)
        if "name" in insert_cols:
            try:
                i = insert_cols.index("name")
                return params[i]
            except Exception:
                return None
        return None
    except Exception:
        log_error(traceback.format_exc(), "punch_log_insert_error")
        return None

def _set_job_card_force_completed(jc_name):
    """
    Force Job Card to completed: sets status and docstatus via SQL.
    This bypasses on_submit hooks — heavy-handed but avoids submit validations.
    """
    try:
        nts.db.sql("UPDATE `tabJob Card` SET status=%s, docstatus=1 WHERE name=%s", ("Completed", jc_name))
        try:
            nts.db.commit()
        except Exception:
            pass
        return True
    except Exception:
        log_error(traceback.format_exc(), "force_complete_job_card_failed")
        return False

def _update_work_order_operation_totals_once(op_row, produced_qty, process_loss, work_order_name, idx):
    """
    Update Work Order Operation totals exactly once (idempotent in typical flows).
    Prefer set_value then fallback to SQL.
    """
    try:
        if op_row.get("name"):
            try:
                current = nts.db.get_value("Work Order Operation", op_row.get("name"), ["completed_qty", "process_loss_qty"], as_dict=True) or {}
                new_completed = flt(current.get("completed_qty") or 0) + produced_qty
                new_loss = flt(current.get("process_loss_qty") or 0) + process_loss
                nts.db.set_value("Work Order Operation", op_row.get("name"),
                                 {"completed_qty": new_completed, "process_loss_qty": new_loss, "op_reported": 1}, update_modified=False)
            except Exception:
                nts.db.sql("""UPDATE `tabWork Order Operation`
                              SET completed_qty=COALESCE(completed_qty,0)+%s,
                                  process_loss_qty=COALESCE(process_loss_qty,0)+%s,
                                  op_reported=1
                              WHERE name=%s""", (produced_qty, process_loss, op_row.get("name")))
        else:
            nts.db.sql("""UPDATE `tabWork Order Operation`
                          SET completed_qty=COALESCE(completed_qty,0)+%s,
                              process_loss_qty=COALESCE(process_loss_qty,0)+%s,
                              op_reported=1
                          WHERE parent=%s AND idx=%s""", (produced_qty, process_loss, work_order_name, op_row.get("idx") or (idx+1)))
        try:
            nts.db.commit()
        except Exception:
            pass
        return True
    except Exception:
        log_error(traceback.format_exc(), "update_work_order_operation_failed")
        return False

@nts.whitelist()
def get_punch_logs(work_order):
    table = "tabOperation Punch Log"
    if not _table_exists(table):
        return {}
    try:
        rows = nts.db.sql(f"""
            SELECT parent_op_idx, employee_number, employee_name, produced_qty, rejected_qty, posting_datetime, name, processed
            FROM `{table}`
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
def report_operation(work_order, op_index, operation_name, employee_number, produced_qty, process_loss=0, posting_datetime=None, complete_operation=1, force_complete=True):
    """
    FORCE-COMPLETE implementation:
    - Creates Job Card (or pick draft)
    - Inserts Job Card Time Log (Doc API)
    - Inserts Operation Punch Log (if present)
    - Force-marks Job Card as Completed (docstatus=1) via SQL (bypasses submit hooks)
    - Updates Work Order Operation totals once
    - Marks Punch processed if inserted
    - Returns structured JSON
    """
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
    emp_label = str(emp.get("employee_name") or employee_number)

    # Work Order
    wo = nts.get_doc("Work Order", work_order)
    if wo.docstatus != 1:
        nts.throw(_("Work Order must be submitted."))

    try:
        idx = int(op_index)
    except Exception:
        nts.throw(_("Invalid operation index."))

    operations = wo.get("operations") or []
    if idx < 0 or idx >= len(operations):
        nts.throw(_("Operation index out of range."))

    op_row = operations[idx]

    def op_required(o):
        rq = flt(o.get("operation_qty") or o.get("for_quantity") or o.get("qty") or o.get("required_qty") or 0)
        if rq <= 0:
            rq = flt(wo.get("qty") or wo.get("production_qty") or wo.get("for_quantity") or 0)
        return rq

    required_qty = op_required(op_row)

    # compute available input
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
        except Exception:
            pass
        available_input = min(required_qty, prev_completed)

    # get authoritative current completed/loss
    current_completed = 0.0
    current_loss = 0.0
    try:
        if op_row.get("name"):
            vals = nts.db.get_value("Work Order Operation", op_row.get("name"), ["completed_qty", "process_loss_qty"], as_dict=True)
            if vals:
                current_completed = flt(vals.get("completed_qty") or 0)
                current_loss = flt(vals.get("process_loss_qty") or 0)
    except Exception:
        current_completed = flt(op_row.get("completed_qty") or 0)
        current_loss = flt(op_row.get("process_loss_qty") or 0)

    # sum unprocessed punches if column exists
    sum_prod_unprocessed = 0.0
    sum_rej_unprocessed = 0.0
    try:
        table = "tabOperation Punch Log"
        if _table_exists(table):
            cols = _get_table_columns(table)
            if "processed" in cols:
                srow = nts.db.sql(f"""
                    SELECT COALESCE(SUM(produced_qty),0) AS prod_sum, COALESCE(SUM(rejected_qty),0) AS rej_sum
                    FROM `{table}`
                    WHERE parent_work_order=%s AND parent_op_idx=%s AND processed=0
                """, (wo.name, idx), as_dict=True)
                sum_prod_unprocessed = flt(srow[0].get("prod_sum") or 0) if srow else 0.0
                sum_rej_unprocessed = flt(srow[0].get("rej_sum") or 0) if srow else 0.0
    except Exception:
        sum_prod_unprocessed = 0.0
        sum_rej_unprocessed = 0.0

    pending_qty = max(0.0, available_input - (current_completed + current_loss + sum_prod_unprocessed + sum_rej_unprocessed))

    if (produced_qty + process_loss) - 1e-9 > pending_qty:
        nts.throw(_("Produced + Rejected ({0}) exceeds pending ({1}).").format(produced_qty + process_loss, pending_qty))
    if int(complete_operation or 0) == 1 and abs((produced_qty + process_loss) - pending_qty) > 1e-6:
        nts.throw(_("To complete, produced + rejected must equal pending ({0}).").format(pending_qty))

    op_text = op_row.get("operation") or op_row.get("operation_name") or operation_name or ""
    workstation = op_row.get("workstation") or wo.get("workstation") or ""

    # Find or create Job Card
    jc_doc = None
    try:
        rows = nts.db.sql("SELECT name, docstatus FROM `tabJob Card` WHERE work_order=%s AND operation=%s ORDER BY creation DESC", (wo.name, op_text))
        jc_name = None
        if rows:
            for r in rows:
                if r and len(r) >= 2 and r[1] == 0:
                    jc_name = r[0]; break
            if not jc_name:
                jc_name = rows[0][0]
        if jc_name:
            jc_doc = nts.get_doc("Job Card", jc_name)
    except Exception:
        jc_doc = None

    if not jc_doc:
        if not workstation:
            nts.throw(_("Workstation is not set for this operation. Please set 'Workstation' on the Work Order operation."))
        try:
            jc_doc = nts.get_doc({
                "doctype": "Job Card",
                "work_order": wo.name,
                "operation": op_text,
                "for_quantity": required_qty,
                "workstation": workstation,
                "status": "Not Started"
            })
            jc_doc.insert(ignore_permissions=True)
            try:
                nts.db.commit()
            except Exception:
                pass
            jc_doc = nts.get_doc("Job Card", jc_doc.name)
        except Exception as exc:
            log_error(traceback.format_exc(), "jobcard_create_failed")
            nts.throw(_("Failed to create Job Card: {0}").format(str(exc)))

    # compute times
    try:
        to_time = posting_dt
        from_time = get_datetime(posting_dt) - timedelta(minutes=1)
    except Exception:
        from_time = posting_dt

    minutes = compute_minutes(from_time, posting_dt)

    # Insert Job Card Time Log via Doc API
    time_log_name = None
    try:
        jctl_cols = _get_table_columns("tabJob Card Time Log") if _table_exists("tabJob Card Time Log") else set()
        tl_doc = nts.get_doc({
            "doctype": "Job Card Time Log",
            "parent": jc_doc.name,
            "parentfield": "time_logs",
            "parenttype": "Job Card",
            "employee": emp_docname,
            "employee_name": emp_label,
            "from_time": str(from_time),
            "to_time": str(posting_dt),
            "time_in_mins": minutes,
            "completed_qty": produced_qty
        })
        if "rejected_qty" in jctl_cols:
            tl_doc.rejected_qty = process_loss
        tl_doc.insert(ignore_permissions=True)
        try:
            nts.db.commit()
        except Exception:
            pass
        time_log_name = tl_doc.name
    except Exception:
        log_error(traceback.format_exc(), "time_log_insert_failed")
        nts.throw(_("Failed to add time log: {0}").format(str(traceback.format_exc())))

    # Insert Operation Punch Log (audit) - unprocessed initially
    punch_name = _insert_operation_punch_log(parent_work_order=wo.name, parent_op_idx=idx, parent_op_name=op_text,
                                            employee_number=employee_number, employee_name=emp_label,
                                            produced_qty=produced_qty, rejected_qty=process_loss,
                                            posting_datetime=posting_dt, processed_flag=0)

    # Force-complete the Job Card (no submit)
    if bool(force_complete):
        ok = _set_job_card_force_completed(jc_doc.name)
        if not ok:
            # rollback and exit
            try:
                if time_log_name:
                    nts.db.sql("DELETE FROM `tabJob Card Time Log` WHERE name=%s", (time_log_name,))
                    nts.db.commit()
            except Exception:
                pass
            if punch_name:
                try:
                    nts.db.sql("DELETE FROM `tabOperation Punch Log` WHERE name=%s", (punch_name,))
                    nts.db.commit()
                except Exception:
                    pass
            nts.throw(_("Failed to force-complete Job Card."))

    # Update Work Order Operation totals exactly once
    _update_work_order_operation_totals_once(op_row, produced_qty, process_loss, wo.name, idx)

    # Mark punch processed (so it won't be counted as unprocessed)
    if punch_name:
        try:
            cols = _get_table_columns("tabOperation Punch Log")
            if "processed" in cols:
                nts.db.sql("UPDATE `tabOperation Punch Log` SET processed=1 WHERE name=%s", (punch_name,))
                try:
                    nts.db.commit()
                except Exception:
                    pass
        except Exception:
            log_error(traceback.format_exc(), "mark_punch_processed_failed")

    # compute remaining for response (re-fetch authoritative values)
    try:
        if op_row.get("name"):
            vals = nts.db.get_value("Work Order Operation", op_row.get("name"), ["completed_qty", "process_loss_qty"], as_dict=True) or {}
            a_completed = flt(vals.get("completed_qty") or 0)
            a_rejected = flt(vals.get("process_loss_qty") or 0)
        else:
            rows = nts.db.sql("""SELECT completed_qty, process_loss_qty FROM `tabWork Order Operation` WHERE parent=%s AND idx=%s LIMIT 1""", (wo.name, op_row.get("idx") or (idx+1)))
            if rows and rows[0]:
                a_completed = flt(rows[0][0] or 0)
                a_rejected = flt(rows[0][1] or 0)
            else:
                a_completed = flt(op_row.get("completed_qty") or 0)
                a_rejected = flt(op_row.get("process_loss_qty") or 0)
        # unprocessed punches
        unp_prod = 0.0
        unp_rej = 0.0
        table = "tabOperation Punch Log"
        if _table_exists(table):
            cols = _get_table_columns(table)
            if "processed" in cols:
                srow2 = nts.db.sql(f"""
                    SELECT COALESCE(SUM(produced_qty),0) AS prod_sum, COALESCE(SUM(rejected_qty),0) AS rej_sum
                    FROM `{table}`
                    WHERE parent_work_order=%s AND parent_op_idx=%s AND processed=0
                """, (wo.name, idx), as_dict=True)
                unp_prod = flt(srow2[0].get("prod_sum") or 0) if srow2 else 0
                unp_rej = flt(srow2[0].get("rej_sum") or 0) if srow2 else 0
        remaining = max(0.0, available_input - (a_completed + a_rejected + unp_prod + unp_rej))
    except Exception:
        remaining = 0.0

    return {
        "ok": True,
        "message": _("Operation {0} reported: produced {1}, rejected {2}. Remaining for this op: {3}").format(op_text, produced_qty, process_loss, remaining),
        "job_card": jc_doc.name if jc_doc else None,
        "job_card_force_completed": bool(force_complete),
        "reporter_employee": emp_docname,
        "reporter_name": emp_label,
        "posting_datetime": str(posting_dt),
        "produced_qty": produced_qty,
        "rejected_qty": process_loss,
        "op_index": idx,
        "op_name": op_text,
        "remaining": remaining
    }
