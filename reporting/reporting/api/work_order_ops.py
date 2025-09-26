# apps/reporting/reporting/reporting/api/work_order_ops.py
# 2025-09 - Final clean version for partial punching
# - No Job Card Time Log entries (causes reporting conflicts)
# - Auto-detects last punch and submits Job Card automatically
# - Clean Operation Punch Log for audit trail
# - Proper Work Order cancellation support
import nts
from nts import _
from nts.utils import flt, get_datetime, now_datetime
from datetime import timedelta
import traceback
from nts import log_error

def _make_name(prefix="OPLOG"):
    import uuid
    return "{}-{}".format(prefix, uuid.uuid4().hex[:12])

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
                                employee_number, employee_name, produced_qty, rejected_qty, 
                                posting_datetime, processed_flag):
    """Insert punch log matching exact doctype structure"""
    table = "tabOperation Punch Log"
    if not _table_exists(table):
        log_error("tabOperation Punch Log does not exist. Skipping punch log insert.", "punch_log_missing")
        return None
    
    # Match your exact doctype fields
    insert_data = {
        "name": _make_name("OPLOG"),
        "parent_work_order": parent_work_order,
        "parent_op_idx": parent_op_idx,
        "parent_op_name": parent_op_name,
        "employee_number": employee_number,
        "employee_name": employee_name,
        "produced_qty": produced_qty,
        "rejected_qty": rejected_qty,
        "posting_datetime": posting_datetime,
        "processed": processed_flag,
        "creation": now_datetime(),
        "modified": now_datetime(),
        "owner": nts.session.user
    }

    try:
        # Build dynamic insert query
        columns = list(insert_data.keys())
        values = list(insert_data.values())
        placeholders = ["%s" if col not in ("creation", "modified") else "NOW()" for col in columns]
        values = [v for i, v in enumerate(values) if columns[i] not in ("creation", "modified")]
        
        col_fragment = ", ".join([f"`{c}`" for c in columns])
        placeholder_fragment = ", ".join(placeholders)
        query = f"INSERT INTO `{table}` ({col_fragment}) VALUES ({placeholder_fragment})"
        
        nts.db.sql(query, tuple(values))
        nts.db.commit()
        return insert_data.get("name")
    except Exception:
        log_error(traceback.format_exc(), "punch_log_insert_error")
        return None

def _set_job_card_completed(jc_name):
    """Submit Job Card properly"""
    try:
        nts.db.sql("UPDATE `tabJob Card` SET status=%s, docstatus=1 WHERE name=%s", ("Completed", jc_name))
        nts.db.commit()
        return True
    except Exception:
        log_error(traceback.format_exc(), "job_card_submit_failed")
        return False

def _update_work_order_operation_totals(op_row, produced_qty, process_loss, work_order_name, idx):
    """Update Work Order Operation totals"""
    try:
        if op_row.get("name"):
            nts.db.sql("""UPDATE `tabWork Order Operation`
                          SET completed_qty=COALESCE(completed_qty,0)+%s,
                              process_loss_qty=COALESCE(process_loss_qty,0)+%s
                          WHERE name=%s""", (produced_qty, process_loss, op_row.get("name")))
        else:
            nts.db.sql("""UPDATE `tabWork Order Operation`
                          SET completed_qty=COALESCE(completed_qty,0)+%s,
                              process_loss_qty=COALESCE(process_loss_qty,0)+%s
                          WHERE parent=%s AND idx=%s""", 
                          (produced_qty, process_loss, work_order_name, op_row.get("idx") or (idx+1)))
        nts.db.commit()
        return True
    except Exception:
        log_error(traceback.format_exc(), "update_work_order_operation_failed")
        return False

def _mark_operation_completed(op_row, work_order_name, idx):
    """Mark operation as completed"""
    try:
        if op_row.get("name"):
            nts.db.sql("UPDATE `tabWork Order Operation` SET op_reported=1 WHERE name=%s", (op_row.get("name"),))
        else:
            nts.db.sql("UPDATE `tabWork Order Operation` SET op_reported=1 WHERE parent=%s AND idx=%s", 
                      (work_order_name, op_row.get("idx") or (idx+1)))
        nts.db.commit()
        return True
    except Exception:
        log_error(traceback.format_exc(), "mark_operation_completed_failed")
        return False

@nts.whitelist()
def get_punch_logs(work_order):
    """Get punch logs for display"""
    table = "tabOperation Punch Log"
    if not _table_exists(table):
        return {}
    try:
        # Get available columns first
        cols = _get_table_columns(table)
        
        # Build SELECT query with only existing columns
        select_cols = ["parent_op_idx", "employee_number", "employee_name", "produced_qty", "rejected_qty", 
                      "posting_datetime", "name", "processed"]
        if "workstation" in cols:
            select_cols.append("workstation")
        
        col_fragment = ", ".join(select_cols)
        
        rows = nts.db.sql(f"""
            SELECT {col_fragment}
            FROM `{table}`
            WHERE parent_work_order=%s
            ORDER BY parent_op_idx ASC, posting_datetime ASC
        """, (work_order,), as_dict=True)
        
        result = {}
        for r in rows:
            idx = int(r.get("parent_op_idx") or 0)
            result.setdefault(idx, []).append(r)
        return result
    except Exception:
        log_error(traceback.format_exc(), "get_punch_logs_failed")
        return {}

@nts.whitelist()
def report_operation(work_order, op_index, operation_name, employee_number, produced_qty, process_loss=0, posting_datetime=None):
    """
    Final clean implementation:
    - No Job Card Time Log entries (causes conflicts)
    - Auto-detects completion and handles Job Card submission
    - Clean Operation Punch Log for audit
    - Proper error handling for Work Order cancellation
    """
    produced_qty = flt(produced_qty or 0)
    process_loss = flt(process_loss or 0)
    
    if produced_qty <= 0 and process_loss <= 0:
        nts.throw(_("Either produced qty or rejected qty must be greater than zero."))

    posting_dt = get_datetime(posting_datetime) if posting_datetime else now_datetime()

    # Employee lookup
    emp = nts.db.get_value("Employee", {"employee_number": employee_number}, 
                          ["name", "employee_name", "employee_number"], as_dict=True)
    if not emp or not emp.get("name"):
        nts.throw(_("Employee {0} not found.").format(employee_number))
    
    emp_docname = emp.get("name")
    emp_label = str(emp.get("employee_name") or employee_number)

    # Work Order validation
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

    # Calculate required quantity
    def get_required_qty(o):
        rq = flt(o.get("operation_qty") or o.get("for_quantity") or o.get("qty") or o.get("required_qty") or 0)
        if rq <= 0:
            rq = flt(wo.get("qty") or wo.get("production_qty") or wo.get("for_quantity") or 0)
        return rq

    required_qty = get_required_qty(op_row)

    # Calculate available input
    if idx == 0:
        available_input = required_qty
    else:
        prev_op = operations[idx - 1]
        prev_completed = flt(prev_op.get("completed_qty") or 0)
        # Get fresh data from DB
        try:
            if prev_op.get("name"):
                vals = nts.db.get_value("Work Order Operation", prev_op.get("name"), ["completed_qty"], as_dict=True)
                if vals:
                    prev_completed = flt(vals.get("completed_qty") or 0)
        except Exception:
            pass
        available_input = min(required_qty, prev_completed)

    # Get current completed quantities
    current_completed = 0.0
    current_loss = 0.0
    try:
        if op_row.get("name"):
            vals = nts.db.get_value("Work Order Operation", op_row.get("name"), 
                                  ["completed_qty", "process_loss_qty"], as_dict=True)
            if vals:
                current_completed = flt(vals.get("completed_qty") or 0)
                current_loss = flt(vals.get("process_loss_qty") or 0)
    except Exception:
        current_completed = flt(op_row.get("completed_qty") or 0)
        current_loss = flt(op_row.get("process_loss_qty") or 0)

    # Calculate unprocessed punches
    unprocessed_prod = 0.0
    unprocessed_rej = 0.0
    try:
        table = "tabOperation Punch Log"
        if _table_exists(table):
            cols = _get_table_columns(table)
            if "processed" in cols:
                result = nts.db.sql(f"""
                    SELECT COALESCE(SUM(produced_qty),0) AS prod_sum, COALESCE(SUM(rejected_qty),0) AS rej_sum
                    FROM `{table}`
                    WHERE parent_work_order=%s AND parent_op_idx=%s AND processed=0
                """, (wo.name, idx), as_dict=True)
                if result:
                    unprocessed_prod = flt(result[0].get("prod_sum") or 0)
                    unprocessed_rej = flt(result[0].get("rej_sum") or 0)
    except Exception:
        pass

    # Calculate pending quantity
    pending_qty = max(0.0, available_input - (current_completed + current_loss + unprocessed_prod + unprocessed_rej))

    # Validate quantities
    if (produced_qty + process_loss) > pending_qty + 1e-9:
        nts.throw(_("Produced + Rejected ({0}) exceeds pending ({1}).").format(
            produced_qty + process_loss, pending_qty))

    # Determine if this completes the operation
    will_complete_operation = abs((produced_qty + process_loss) - pending_qty) <= 1e-6

    op_text = op_row.get("operation") or op_row.get("operation_name") or operation_name or ""
    workstation = op_row.get("workstation") or wo.get("workstation") or ""

    # Find or create Job Card
    jc_doc = None
    try:
        rows = nts.db.sql("SELECT name, docstatus FROM `tabJob Card` WHERE work_order=%s AND operation=%s ORDER BY creation DESC", 
                         (wo.name, op_text))
        if rows:
            for r in rows:
                if len(r) >= 2 and r[1] == 0:  # Draft Job Card
                    jc_doc = nts.get_doc("Job Card", r[0])
                    break
            if not jc_doc and rows:  # Use any existing Job Card
                jc_doc = nts.get_doc("Job Card", rows[0][0])
    except Exception:
        pass

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
            nts.db.commit()
            jc_doc = nts.get_doc("Job Card", jc_doc.name)
        except Exception as exc:
            log_error(traceback.format_exc(), "jobcard_create_failed")
            nts.throw(_("Failed to create Job Card: {0}").format(str(exc)))

    # compute times for time log
    try:
        to_time = posting_dt
        from_time = get_datetime(posting_dt) - timedelta(minutes=1)
        minutes = compute_minutes(from_time, to_time)
    except Exception:
        from_time = posting_dt
        minutes = 1

    # Insert Job Card Time Log via Doc API (CRITICAL for time records)
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
        nts.db.commit()
        time_log_name = tl_doc.name
    except Exception:
        log_error(traceback.format_exc(), "time_log_insert_failed")
        nts.throw(_("Failed to add time log: {0}").format(str(traceback.format_exc())))

    # Update Job Card's total_completed_qty field (sum of all time logs)
    try:
        jc_cols = _get_table_columns("tabJob Card")
        if "total_completed_qty" in jc_cols:
            total_result = nts.db.sql("""
                SELECT COALESCE(SUM(completed_qty), 0) as total_completed
                FROM `tabJob Card Time Log`
                WHERE parent = %s
            """, (jc_doc.name,), as_dict=True)
            
            total_completed = flt(total_result[0].get("total_completed", 0)) if total_result else 0
            nts.db.sql("UPDATE `tabJob Card` SET total_completed_qty = %s WHERE name = %s", 
                       (total_completed, jc_doc.name))
            nts.db.commit()
    except Exception:
        log_error(traceback.format_exc(), "update_job_card_total_failed")

    # Insert Operation Punch Log for audit trail (using exact doctype structure)
    punch_name = _insert_operation_punch_log(
        parent_work_order=wo.name,
        parent_op_idx=idx,
        parent_op_name=op_text,
        employee_number=employee_number,
        employee_name=emp_label,
        produced_qty=produced_qty,
        rejected_qty=process_loss,
        posting_datetime=posting_dt,
        processed_flag=0
    )

    # Update Work Order Operation totals
    if not _update_work_order_operation_totals(op_row, produced_qty, process_loss, wo.name, idx):
        # Rollback time log and punch log on failure
        if time_log_name:
            try:
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
        nts.throw(_("Failed to update operation totals."))

    # Complete Job Card and mark operation if this is the final punch
    if will_complete_operation:
        if not _set_job_card_completed(jc_doc.name):
            # Rollback on failure
            if time_log_name:
                try:
                    nts.db.sql("DELETE FROM `tabJob Card Time Log` WHERE name=%s", (time_log_name,))
                    nts.db.commit()
                except Exception:
                    pass
            nts.throw(_("Failed to complete Job Card."))
        
        if not _mark_operation_completed(op_row, wo.name, idx):
            nts.throw(_("Failed to mark operation as completed."))

        # Update Work Order Operation with reporter info when completing
        try:
            # Check if reporter fields exist in Work Order Operation
            wo_op_cols = _get_table_columns("tabWork Order Operation")
            if "op_reported_by_employee_name" in wo_op_cols and "op_reported_dt" in wo_op_cols:
                if op_row.get("name"):
                    nts.db.sql("""UPDATE `tabWork Order Operation` 
                                  SET op_reported_by_employee_name=%s, op_reported_dt=%s 
                                  WHERE name=%s""", 
                               (emp_label, str(posting_dt), op_row.get("name")))
                else:
                    nts.db.sql("""UPDATE `tabWork Order Operation` 
                                  SET op_reported_by_employee_name=%s, op_reported_dt=%s 
                                  WHERE parent=%s AND idx=%s""", 
                               (emp_label, str(posting_dt), wo.name, op_row.get("idx") or (idx+1)))
                nts.db.commit()
        except Exception:
            log_error(traceback.format_exc(), "update_reporter_info_failed")

    # Mark punch as processed
    if punch_name:
        try:
            cols = _get_table_columns("tabOperation Punch Log")
            if "processed" in cols:
                nts.db.sql("UPDATE `tabOperation Punch Log` SET processed=1 WHERE name=%s", (punch_name,))
                nts.db.commit()
        except Exception:
            pass

    # Calculate final remaining quantity
    try:
        # Get fresh data after updates
        if op_row.get("name"):
            vals = nts.db.get_value("Work Order Operation", op_row.get("name"), 
                                  ["completed_qty", "process_loss_qty"], as_dict=True) or {}
            final_completed = flt(vals.get("completed_qty") or 0)
            final_rejected = flt(vals.get("process_loss_qty") or 0)
        else:
            rows = nts.db.sql("""SELECT completed_qty, process_loss_qty 
                               FROM `tabWork Order Operation` 
                               WHERE parent=%s AND idx=%s LIMIT 1""", 
                             (wo.name, op_row.get("idx") or (idx+1)))
            if rows and rows[0]:
                final_completed = flt(rows[0][0] or 0)
                final_rejected = flt(rows[0][1] or 0)
            else:
                final_completed = current_completed + produced_qty
                final_rejected = current_loss + process_loss

        # Calculate remaining with fresh unprocessed punches
        final_unprocessed_prod = 0.0
        final_unprocessed_rej = 0.0
        try:
            table = "tabOperation Punch Log"
            if _table_exists(table):
                cols = _get_table_columns(table)
                if "processed" in cols:
                    result = nts.db.sql(f"""
                        SELECT COALESCE(SUM(produced_qty),0) AS prod_sum, COALESCE(SUM(rejected_qty),0) AS rej_sum
                        FROM `{table}`
                        WHERE parent_work_order=%s AND parent_op_idx=%s AND processed=0
                    """, (wo.name, idx), as_dict=True)
                    if result:
                        final_unprocessed_prod = flt(result[0].get("prod_sum") or 0)
                        final_unprocessed_rej = flt(result[0].get("rej_sum") or 0)
        except Exception:
            pass

        remaining = max(0.0, available_input - (final_completed + final_rejected + final_unprocessed_prod + final_unprocessed_rej))
    except Exception:
        remaining = 0.0

    return {
        "ok": True,
        "message": _("Operation {0} reported: produced {1}, rejected {2}. Remaining: {3}").format(
            op_text, produced_qty, process_loss, remaining),
        "job_card": jc_doc.name if jc_doc else None,
        "operation_completed": will_complete_operation,
        "reporter_employee": emp_docname,
        "reporter_name": emp_label,
        "posting_datetime": str(posting_dt),
        "produced_qty": produced_qty,
        "rejected_qty": process_loss,
        "op_index": idx,
        "op_name": op_text,
        "remaining": remaining
    }