// apps/reporting/reporting/public/js/work_order_ops_report.js
// Renders operations table with merged operation-name cell and per-punch rows beneath.
// Reporter column enlarged; shows only employee_name (truncated) or employee_number if name missing.

nts.provide("reporting_ops");
(function() {
  if (!document.getElementById("reporting-custom-css")) {
    const s = document.createElement("style");
    s.id = "reporting-custom-css";
    s.innerHTML = `
      .r-report-table{width:100%;border-collapse:collapse;font-family:Arial;margin:6px 0 14px 0}
      .r-report-table th,.r-report-table td{border:1px solid #e0e6ef;padding:8px;vertical-align:middle}
      .r-report-table th{background:#f7f9fb;font-weight:600;color:#333}
      .r-report-btn{padding:6px 12px;background:#007bff;color:#fff;border-radius:6px;border:0;cursor:pointer;font-size:13px}
      .r-small-muted{color:#777;font-size:0.95em}
      .r-col-center{text-align:center}
      /* tuned widths */
      .r-col-num{width:46px}
      .r-col-op{width:260px}
      .r-col-com{width:86px}
      .r-col-rej{width:86px}
      .r-col-ws{width:120px}
      .r-col-rep{width:240px} /* reporter column larger */
      .r-col-date{width:150px}
      .r-reporter-cell{font-weight:600}
      .r-empty-cell{background:#fafafa}
      .r-punch-log-item{font-size:0.92em;color:#444;padding:6px 0}
      .r-punch-sub{color:#666;font-size:0.85em;margin-top:4px}
      .r-report-note{color:#666;font-size:0.95em;margin-top:8px}
    `;
    document.head.appendChild(s);
  }

  const HOST_FIELD = "r_operations_reporting_html";
  nts.ui.form.on("Work Order", { refresh: function(frm) { render(frm); } });

  function flt_zero(v) { return (typeof v === "number") ? v : (parseFloat(v) || 0); }
  function escapeHtml(s) { if (!s && s !== 0) return ""; return String(s).replace(/[&<>"'`=\/]/g, ch => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;","/":"&#x2F;","`":"&#x60;","=":"&#x3D;" })[ch]); }
  function required(op, wo) { let q = flt_zero(op.operation_qty || op.for_quantity || op.qty || op.required_qty); if (q > 0) return q; return flt_zero(wo.qty || wo.production_qty || wo.for_quantity || wo.qty_to_manufacture); }

  function render(frm) {
    if (!frm.fields_dict || !frm.fields_dict[HOST_FIELD]) return;
    const ops = frm.doc.operations || [];
    if (!ops.length) { frm.fields_dict[HOST_FIELD].html("<div>No operations</div>"); return; }

    // fetch punch logs first
    nts.call({
      method: "reporting.reporting.api.work_order_ops.get_punch_logs",
      args: { work_order: frm.doc.name },
      callback: function(r) {
        const logs_map = (r && r.message) ? r.message : {};
        build_table(frm, logs_map);
      },
      error: function() {
        build_table(frm, {});
      }
    });
  }

  function build_table(frm, logs_map) {
    const ops = frm.doc.operations || [];
    const started = !!frm.doc.material_transferred_for_manufacturing;

    // compute first pending op
    let first_pending = null;
    for (let i = 0; i < ops.length; i++) {
      const o = ops[i];
      const req = required(o, frm.doc);
      const done = flt_zero(o.completed_qty) + flt_zero(o.process_loss_qty);
      if (!o.op_reported && done < req - 1e-9) { first_pending = i; break; }
    }

    let h = `<table class="r-report-table"><thead><tr>
      <th class="r-col-num">#</th>
      <th class="r-col-op">Operation</th>
      <th class="r-col-com">Completed</th>
      <th class="r-col-rej">Rejected</th>
      <th class="r-col-ws">Workstation</th>
      <th class="r-col-rep">Reporter</th>
      <th class="r-col-date">Reported At</th>
      <th class="r-col-date">Action</th>
    </tr></thead><tbody>`;

    ops.forEach((o, idx) => {
      const req = required(o, frm.doc);
      const done = flt_zero(o.completed_qty) + flt_zero(o.process_loss_qty);
      let pending = 0;
      if (idx === 0) pending = Math.max(0, req - done);
      else {
        const prev = (frm.doc.operations || [])[idx - 1] || {};
        const prev_completed = flt_zero(prev.completed_qty);
        pending = Math.max(0, prev_completed - done);
      }
      const show_btn = started && first_pending === idx && !o.op_reported && pending > 0;

      const punches = logs_map[idx] || [];

      // main row with operation name (rowspan punches_count + 1)
      h += `<tr data-idx="${idx}">`;
      h += `<td class="r-col-num" rowspan="${punches.length + 1}">${o.idx || idx+1}</td>`;
      h += `<td class="r-col-op" rowspan="${punches.length + 1}">${escapeHtml(o.operation || "")}</td>`;
      h += `<td class="r-col-com">${o.completed_qty || 0}</td>`;
      h += `<td class="r-col-rej">${o.process_loss_qty || 0}</td>`;
      h += `<td class="r-col-ws">${escapeHtml(o.workstation || "")}</td>`;
      const rep_summary = o.op_reported_by_employee_name || "";
      const rep_display = rep_summary.length > 28 ? escapeHtml(rep_summary.substring(0, 25) + "...") : escapeHtml(rep_summary || "—");
      h += `<td class="r-reporter-cell">${rep_display}</td>`;
      h += `<td class="r-col-date">${escapeHtml(o.op_reported_dt || "—")}</td>`;
      h += `<td class="r-col-date">${show_btn?`<button class="r-report-btn" data-idx="${idx}">Report (${pending} left)</button>`:"—"}</td>`;
      h += `</tr>`;

      // punch rows (one per punch)
      if (punches.length) {
        punches.forEach(function(p) {
          const name_only = (p.employee_name && p.employee_name.trim()) ? p.employee_name.trim() : (p.employee_number || "");
          const display_name = name_only.length > 28 ? name_only.substring(0, 25) + "..." : name_only;
          h += `<tr class="r-punch-row">`;
          h += `<td class="r-col-com">${p.produced_qty || 0}</td>`;
          h += `<td class="r-col-rej">${p.rejected_qty || 0}</td>`;
          h += `<td class="r-col-ws r-empty-cell"></td>`;
          h += `<td class="r-reporter-cell">${escapeHtml(display_name || "—")}</td>`;
          h += `<td class="r-col-date">${escapeHtml(p.posting_datetime || "")}</td>`;
          h += `<td class="r-col-date">—</td>`;
          h += `</tr>`;
        });
      } else {
        h += `<tr class="r-punch-row">`;
        h += `<td class="r-col-com">—</td>`;
        h += `<td class="r-col-rej">—</td>`;
        h += `<td class="r-col-ws r-empty-cell"></td>`;
        h += `<td class="r-reporter-cell">—</td>`;
        h += `<td class="r-col-date">—</td>`;
        h += `<td class="r-col-date">—</td>`;
        h += `</tr>`;
      }
    });

    h += `</tbody></table>`;
    h += `<div class="r-report-note">Click Report for the next pending operation. Produced may be 0 when everything is rejected. Partial punching supported. Multiple operators shown as separate rows under each operation.</div>`;

    frm.fields_dict[HOST_FIELD].html(h);

    // attach actions
    const $wrap = frm.fields_dict[HOST_FIELD].$wrapper;
    $wrap.find(".r-report-btn").off("click").on("click", function() {
      const idx = parseInt(this.getAttribute("data-idx"), 10);
      frm.reload_doc().then(() => {
        const fresh = cur_frm.doc.operations || [];
        let fp = null;
        for (let i = 0; i < fresh.length; i++) {
          const o = fresh[i];
          const req = required(o, cur_frm.doc);
          const done = flt_zero(o.completed_qty) + flt_zero(o.process_loss_qty);
          if (!o.op_reported && done < req - 1e-9) { fp = i; break; }
        }
        if (fp === null) { nts.msgprint("All operations already reported."); frm.reload_doc(); return; }
        if (fp !== idx) { nts.msgprint("This operation is no longer the next pending operation. UI refreshed."); frm.reload_doc(); return; }
        const op = fresh[idx];
        if (!op) { nts.msgprint("Operation not found."); frm.reload_doc(); return; }
        open_dialog(frm, op, idx);
      }).catch(() => { nts.msgprint("Unable to refresh Work Order. Try again."); });
    });
  }

  function open_dialog(frm, op, idx) {
    const req = required(op, frm.doc);
    const done = flt_zero(op.completed_qty) + flt_zero(op.process_loss_qty);
    let pending;
    if (idx === 0) pending = Math.max(0, req - done);
    else {
      const prev = (frm.doc.operations || [])[idx - 1] || {};
      const prev_completed = flt_zero(prev.completed_qty);
      pending = Math.max(0, prev_completed - done);
    }

    const d = new nts.ui.Dialog({
      title: "Report " + (op.operation || ""),
      fields: [
        {label: "Employee Number", fieldname: "empno", fieldtype: "Data", reqd: 1},
        {label: "Employee Name", fieldname: "empname", fieldtype: "Data", read_only: 1},
        {label: "Produced", fieldname: "prod", fieldtype: "Float", default: pending},
        {label: "Rejected", fieldname: "rej", fieldtype: "Float", default: 0},
        {label: "Complete Operation", fieldname: "complete", fieldtype: "Check", description: "Check only when produced+rejected equals remaining pending"}
      ],
      primary_action_label: "Submit",
      primary_action: function(values) {
        if (!values || !values.empno) { nts.msgprint("Employee number is required."); return; }
        const produced = flt_zero(values.prod);
        const rej = flt_zero(values.rej);
        if (produced <= 0 && rej <= 0) { nts.msgprint("Enter produced or rejected quantity."); return; }
        if (produced + rej > pending + 1e-9) { nts.msgprint("Produced + Rejected exceeds pending."); return; }
        if (values.complete && Math.abs((produced + rej) - pending) > 1e-6) { nts.msgprint("To mark complete, produced+rejected must equal remaining pending."); return; }

        const workstation = op.workstation || "";
        if (workstation) {
          nts.call({
            method: "reporting.reporting.api.work_order_ops.get_workstation_allowed",
            args: { workstation: workstation },
            callback: function(resp) {
              try {
                const raw = resp && resp.message ? resp.message : "";
                if (raw && String(raw).trim().length) {
                  const arr = String(raw).replace(/;/g, ",").split(",").map(x => x.trim().toLowerCase()).filter(x => x);
                  const empnum = String(values.empno).trim().toLowerCase();
                  nts.db.get_value("Employee", {"employee_number": values.empno}, ["name", "employee_name"]).then(r2 => {
                    const empname = (r2 && r2.message && r2.message.employee_name) ? String(r2.message.employee_name).trim().toLowerCase() : "";
                    const empdoc = (r2 && r2.message && r2.message.name) ? String(r2.message.name).trim().toLowerCase() : "";
                    if (arr.indexOf(empnum) === -1 && arr.indexOf(empname) === -1 && arr.indexOf(empdoc) === -1) {
                      nts.msgprint("You are not authorized to report on this workstation.");
                      return;
                    }
                    submit_report(frm, op, idx, values, pending, d);
                  }).catch(() => { submit_report(frm, op, idx, values, pending, d); });
                } else {
                  submit_report(frm, op, idx, values, pending, d);
                }
              } catch (e) { submit_report(frm, op, idx, values, pending, d); }
            },
            error: function() { submit_report(frm, op, idx, values, pending, d); }
          });
        } else {
          submit_report(frm, op, idx, values, pending, d);
        }
      }
    });

    d.get_field("empno").$input.on("change", function() {
      const v = d.get_value("empno");
      if (!v) { d.set_value("empname", ""); return; }
      nts.db.get_value("Employee", {"employee_number": v}, ["employee_name"]).then(r => {
        const name = (r && r.message && r.message.employee_name) ? r.message.employee_name : "";
        d.set_value("empname", name);
      }).catch(() => d.set_value("empname", ""));
    });

    d.show();
  }

  function submit_report(frm, op, idx, values, pending, dialog) {
    const produced = flt_zero(values.prod);
    const rej = flt_zero(values.rej);
    nts.call({
      method: "reporting.reporting.api.work_order_ops.report_operation",
      args: {
        work_order: frm.doc.name,
        op_index: idx,
        operation_name: op.operation || "",
        employee_number: values.empno,
        produced_qty: produced,
        process_loss: rej,
        posting_datetime: nts.datetime.now_datetime(),
        complete_operation: values.complete ? 1 : 0
      },
      freeze: true,
      freeze_message: "Reporting...",
      callback: function(r) {
        if (r && r.message) nts.msgprint(r.message); else nts.msgprint("Reported.");
        dialog.hide();
        frm.reload_doc();
      },
      error: function(err) {
        try {
          const srv = err && err.responseJSON && err.responseJSON._server_messages ? JSON.parse(err.responseJSON._server_messages)[0] : (err && err.responseJSON && err.responseJSON.message ? err.responseJSON.message : null);
          if (srv) { nts.msgprint(String(srv)); } else { nts.msgprint("Failed to report. Contact admin."); }
        } catch (e) {
          nts.msgprint("Failed to report. Contact admin.");
        }
        dialog.hide();
        frm.reload_doc();
      }
    });
  }

})();
