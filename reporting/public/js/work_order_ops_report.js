// apps/reporting/reporting/public/js/work_order_ops_report.js
// Fixed UI for proper partial punching support
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
      .r-col-num{width:46px}
      .r-col-op{width:260px}
      .r-col-com{width:86px}
      .r-col-rej{width:86px}
      .r-col-ws{width:120px}
      .r-col-rep{width:240px}
      .r-col-date{width:150px}
      .r-reporter-cell{font-weight:600}
      .r-empty-cell{background:#fafafa}
      .r-punch-log-item{font-size:0.92em;color:#444;padding:6px 0}
      .r-report-note{color:#666;font-size:0.95em;margin-top:8px}
      .r-operation-completed{background-color:#e8f5e8;}
      .r-operation-partial{background-color:#fff3cd;}
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

    // Find first operation with remaining quantity (not necessarily the first non-reported one)
    let first_pending = null;
    for (let i = 0; i < ops.length; i++) {
      const o = ops[i];
      const req = required(o, frm.doc);
      const done = flt_zero(o.completed_qty) + flt_zero(o.process_loss_qty);
      
      // Check if this operation has remaining quantity
      let pending;
      if (i === 0) {
        pending = Math.max(0, req - done);
      } else {
        const prev = ops[i - 1] || {};
        const prev_completed = flt_zero(prev.completed_qty);
        pending = Math.max(0, prev_completed - done);
      }
      
      if (pending > 1e-9) { 
        first_pending = i; 
        break; 
      }
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
      let pending;
      if (idx === 0) {
        pending = Math.max(0, req - done);
      } else {
        const prev = (frm.doc.operations || [])[idx - 1] || {};
        const prev_completed = flt_zero(prev.completed_qty);
        pending = Math.max(0, prev_completed - done);
      }
      
      const show_btn = started && first_pending === idx && pending > 1e-9;
      const punches = logs_map[idx] || [];
      const is_completed = o.op_reported || (pending <= 1e-9);
      const row_class = is_completed ? "r-operation-completed" : (done > 1e-9 ? "r-operation-partial" : "");

      h += `<tr data-idx="${idx}" class="${row_class}">`;
      h += `<td class="r-col-num" rowspan="${Math.max(1, punches.length) + 1}">${o.idx || idx+1}</td>`;
      h += `<td class="r-col-op" rowspan="${Math.max(1, punches.length) + 1}">${escapeHtml(o.operation || "")}</td>`;
      h += `<td class="r-col-com">${o.completed_qty || 0}</td>`;
      h += `<td class="r-col-rej">${o.process_loss_qty || 0}</td>`;
      h += `<td class="r-col-ws">${escapeHtml(o.workstation || "")}</td>`;
      const rep_summary = o.op_reported_by_employee_name || "";
      const rep_display = rep_summary.length > 28 ? escapeHtml(rep_summary.substring(0, 25) + "...") : escapeHtml(rep_summary || "—");
      h += `<td class="r-reporter-cell">${rep_display}</td>`;
      h += `<td class="r-col-date">${escapeHtml(o.op_reported_dt || "—")}</td>`;
      h += `<td class="r-col-date">${show_btn?`<button class="r-report-btn" data-idx="${idx}">Report (${pending.toFixed(1)} left)</button>`:"—"}</td>`;
      h += `</tr>`;

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
    h += `<div class="r-report-note">Click Report for operations with remaining quantities. Partial punching supported - operation stays open until explicitly completed. Green rows are completed operations, yellow rows have partial progress.</div>`;

    frm.fields_dict[HOST_FIELD].html(h);

    const $wrap = frm.fields_dict[HOST_FIELD].$wrapper;
    $wrap.find(".r-report-btn").off("click").on("click", function() {
      const idx = parseInt(this.getAttribute("data-idx"), 10);
      // reload doc lightly then show dialog
      frm.reload_doc().then(() => {
        const fresh = cur_frm.doc.operations || [];
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
    if (idx === 0) {
      pending = Math.max(0, req - done);
    } else {
      const prev = (frm.doc.operations || [])[idx - 1] || {};
      const prev_completed = flt_zero(prev.completed_qty);
      pending = Math.max(0, prev_completed - done);
    }

    const d = new nts.ui.Dialog({
      title: "Report " + (op.operation || ""),
      fields: [
        {label: "Employee Number", fieldname: "empno", fieldtype: "Data", reqd: 1},
        {label: "Employee Name", fieldname: "empname", fieldtype: "Data", read_only: 1},
        {label: "Produced", fieldname: "prod", fieldtype: "Float", default: 0},
        {label: "Rejected", fieldname: "rej", fieldtype: "Float", default: 0},
        {label: "Complete Operation", fieldname: "complete", fieldtype: "Check", description: "Check only when this punch completes the entire operation"},
        {label: "Force Complete JC", fieldname: "force", fieldtype: "Check", default: 1, description: "Force-complete Job Card when operation is completed"}
      ],
      primary_action_label: "Submit",
      primary_action: function(values) {
        if (!values || !values.empno) { nts.msgprint("Employee number is required."); return; }
        const produced = flt_zero(values.prod);
        const rej = flt_zero(values.rej);
        if (produced <= 0 && rej <= 0) { nts.msgprint("Enter produced or rejected quantity."); return; }
        if (produced + rej > pending + 1e-9) { nts.msgprint("Produced + Rejected exceeds pending."); return; }
        if (values.complete && Math.abs((produced + rej) - pending) > 1e-6) { 
          nts.msgprint("To mark complete, produced+rejected must equal remaining pending (" + pending.toFixed(2) + ")."); 
          return; 
        }

        submit_report(frm, op, idx, values, pending, d);
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

    // Set default produced quantity to remaining if user wants to complete the operation
    d.get_field("complete").$input.on("change", function() {
      if (d.get_value("complete")) {
        const current_prod = flt_zero(d.get_value("prod"));
        const current_rej = flt_zero(d.get_value("rej"));
        const total_current = current_prod + current_rej;
        if (total_current < pending) {
          d.set_value("prod", pending - current_rej);
        }
      }
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
        complete_operation: values.complete ? 1 : 0,
        force_complete: values.force ? 1 : 0
      },
      freeze: true,
      freeze_message: "Reporting...",
      callback: function(r) {
        try {
          const resp = r && r.message ? r.message : (r || {});
          if (resp.ok) {
            dialog.hide();
            let msg = resp.message || "Reported.";
            if (resp.remaining > 1e-9) {
              msg += " Operation remains open for more punches.";
            } else if (resp.operation_completed) {
              msg += " Operation completed!";
            }
            nts.msgprint(msg);
            // refresh authoritative doc values to avoid doubling / ghost rows
            frm.reload_doc();
          } else {
            dialog.hide();
            const err = resp.error_message || "Reporting failed.";
            const tb = resp.traceback;
            if (tb) {
              const d = new nts.ui.Dialog({
                title: "Server Error",
                fields: [{label:"Error", fieldname:"err", fieldtype:"Small Text", read_only:1},{label:"Trace", fieldname:"tb", fieldtype:"Code", read_only:1}],
                primary_action_label: "Close",
                primary_action: function(){ d.hide(); }
              });
              d.set_value("err", err);
              d.set_value("tb", tb);
              d.show();
            } else {
              nts.msgprint(err);
            }
            frm.reload_doc();
          }
        } catch (e) {
          dialog.hide();
          nts.msgprint("Reported (UI update failed).");
          frm.reload_doc();
        }
      },
      error: function(err) {
        dialog.hide();
        try {
          const srv = err && err.responseJSON && err.responseJSON._server_messages ? JSON.parse(err.responseJSON._server_messages)[0] : (err && err.responseJSON && err.responseJSON.message ? err.responseJSON.message : null);
          if (srv) { nts.msgprint(String(srv)); } else { nts.msgprint("Failed to report. Contact admin."); }
        } catch (e) {
          nts.msgprint("Failed to report. Contact admin.");
        }
        frm.reload_doc();
      }
    });
  }

})();