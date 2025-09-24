// apps/reporting/reporting/public/js/work_order_ops_report.js
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
      .r-col-narrow{width:160px;white-space:nowrap;text-align:center}
      .r-report-note{color:#666;font-size:0.95em;margin-top:8px}
      .r-rep-header{display:flex;align-items:center;gap:12px;margin-bottom:8px}
      .r-punch-log{font-size:0.9em;margin-top:6px;padding-left:14px;color:#444}
      .r-punch-log-item{margin:3px 0;border-left:2px solid #eee;padding-left:8px}
      .r-punch-small{color:#666;font-size:0.85em}
    `;
    document.head.appendChild(s);
  }

  (function() {
    const orig = nts.msgprint;
    nts.msgprint = function(msg, title) {
      try {
        if (typeof msg === "string" && /job\s*card.*created/i.test(msg)) return;
        if (typeof msg === "string" && /job\s*card.*created/i.test(JSON.stringify(msg))) return;
      } catch (e) {}
      return orig.apply(this, arguments);
    };
  })();

  const HOST_FIELD = "r_operations_reporting_html";
  nts.ui.form.on("Work Order", { refresh: function(frm) { render(frm); } });

  function flt_zero(v) { return (typeof v === "number") ? v : (parseFloat(v) || 0); }
  function escapeHtml(s) { if (!s && s !== 0) return ""; return String(s).replace(/[&<>"'`=\/]/g, ch => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;","/":"&#x2F;","`":"&#x60;","=":"&#x3D;" })[ch]); }
  function required(op, wo) { let q = flt_zero(op.operation_qty || op.for_quantity || op.qty || op.required_qty); if (q > 0) return q; return flt_zero(wo.qty || wo.production_qty || wo.for_quantity || wo.qty_to_manufacture); }

  function render(frm) {
    if (!frm.fields_dict || !frm.fields_dict[HOST_FIELD]) return;
    const ops = frm.doc.operations || [];
    if (!ops.length) { frm.fields_dict[HOST_FIELD].html("<div>No operations</div>"); return; }
    const started = !!frm.doc.material_transferred_for_manufacturing;

    // compute first pending op
    let first_pending = null;
    for (let i = 0; i < ops.length; i++) {
      const o = ops[i];
      const req = required(o, frm.doc);
      const done = flt_zero(o.completed_qty) + flt_zero(o.process_loss_qty);
      if (!o.op_reported && done < req - 1e-9) { first_pending = i; break; }
    }

    let h = `<table class="r-report-table"><thead><tr><th>#</th><th>Operation</th><th class="r-col-narrow">Completed</th><th class="r-col-narrow">Rejected</th><th>Workstation</th><th class="r-col-narrow">Reporter</th><th class="r-col-narrow">Reported At</th><th class="r-col-narrow">Action</th></tr></thead><tbody>`;
    ops.forEach((o, i) => {
      const reporter_name = o.op_reported_by_employee_name || "";
      const reported_at = o.op_reported_dt || "";
      const req = required(o, frm.doc);
      const done = flt_zero(o.completed_qty) + flt_zero(o.process_loss_qty);
      // pending logic: first op uses required; downstream uses prev.completed_qty
      let pending = 0;
      if (i === 0) {
        pending = Math.max(0, req - done);
      } else {
        const prev = (frm.doc.operations || [])[i - 1] || {};
        const prev_completed = flt_zero(prev.completed_qty);
        pending = Math.max(0, prev_completed - done);
      }
      const show_btn = started && first_pending === i && !o.op_reported && pending > 0;
      h += `<tr data-idx="${i}"><td>${o.idx || i+1}</td><td>${escapeHtml(o.operation||"")}</td><td class="r-col-center">${o.completed_qty||0}</td><td class="r-col-center">${o.process_loss_qty||0}</td><td>${escapeHtml(o.workstation||"")}</td><td class="r-col-center reporter-cell" data-emp="${escapeHtml(o.op_reported_by_employee||"")}">${escapeHtml(reporter_name||"") || "—"}</td><td class="r-col-center">${escapeHtml(reported_at||"") || "—"}</td><td class="r-col-center">${show_btn?`<button class="r-report-btn" data-idx="${i}">Report (${pending} left)</button>`:"—"}</td></tr>`;
      h += `<tr class="r-punch-row" data-idx="${i}"><td colspan="8"><div class="r-punch-log" id="r-punch-log-${i}">Loading punch log...</div></td></tr>`;
    });
    h += `</tbody></table>`;
    if (!started) h += `<div class="r-report-note">Work Order is not started. Transfer materials to WIP to enable reporting.</div>`; else h += `<div class="r-report-note">Click Report for the next pending operation. Enter produced and rejected. Partial punching supported. Multiple operators allowed — audit records shown below each operation.</div>`;
    frm.fields_dict[HOST_FIELD].html(h);

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

    // fetch punch logs
    nts.call({
      method: "reporting.reporting.api.work_order_ops.get_punch_logs",
      args: { work_order: frm.doc.name },
      callback: function(r) {
        try {
          const logs_map = r.message || {};
          Object.keys(logs_map).forEach(k => {
            const idx = parseInt(k, 10);
            const container = document.getElementById("r-punch-log-" + idx);
            if (!container) return;
            const items = logs_map[k] || [];
            if (!items.length) {
              container.innerHTML = `<div class="r-punch-small">No punches yet.</div>`;
            } else {
              let html = "";
              items.forEach(it => {
                const en = escapeHtml(it.employee_number || "");
                const nm = escapeHtml(it.employee_name || "");
                const p = parseFloat(it.produced_qty||0);
                const rej = parseFloat(it.rejected_qty||0);
                const dt = escapeHtml(it.posting_datetime || "");
                html += `<div class="r-punch-log-item"><strong>${en}${nm? " - " + nm : ""}</strong> — produced: ${p}, rejected: ${rej}<div class="r-punch-small">${dt}</div></div>`;
              });
              container.innerHTML = html;
            }
          });
        } catch (e) {
          const placeholders = document.querySelectorAll("[id^='r-punch-log-']");
          placeholders.forEach(p => { p.innerHTML = `<div class="r-punch-small">Punch log unavailable</div>`; });
        }
      },
      error: function() {
        const placeholders = document.querySelectorAll("[id^='r-punch-log-']");
        placeholders.forEach(p => { p.innerHTML = `<div class="r-punch-small">Punch log unavailable</div>`; });
      }
    });

    fill_missing_reporter_names(frm);
  }

  function fill_missing_reporter_names(frm) {
    const $cells = frm.fields_dict[HOST_FIELD].$wrapper.find(".reporter-cell");
    $cells.each(function() {
      const $this = $(this);
      const emp = $this.attr("data-emp");
      if (!emp) return;
      if ($this.text().trim() && $this.text().trim() !== "—") return;
      nts.db.get_value("Employee", emp, ["employee_name"]).then(r => {
        const name = (r && r.message && r.message.employee_name) ? r.message.employee_name : emp;
        $this.text(name);
      }).catch(() => { $this.text(emp); });
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
          // call server helper to get allowed tokens (safe)
          nts.call({
            method: "reporting.reporting.api.work_order_ops.get_workstation_allowed",
            args: { workstation: workstation },
            callback: function(resp) {
              try {
                const raw = resp && resp.message ? resp.message : "";
                if (raw && String(raw).trim().length) {
                  const arr = String(raw).replace(/;/g, ",").split(",").map(x => x.trim().toLowerCase()).filter(x => x);
                  const empnum = String(values.empno).trim().toLowerCase();
                  // get employee docname + name if present (best-effort)
                  nts.db.get_value("Employee", {"employee_number": values.empno}, ["name", "employee_name"]).then(r2 => {
                    const empname = (r2 && r2.message && r2.message.employee_name) ? String(r2.message.employee_name).trim().toLowerCase() : "";
                    const empdoc = (r2 && r2.message && r2.message.name) ? String(r2.message.name).trim().toLowerCase() : "";
                    if (arr.indexOf(empnum) === -1 && arr.indexOf(empname) === -1 && arr.indexOf(empdoc) === -1) { nts.msgprint("You are not authorized to report on this workstation."); return; }
                    submit_report(frm, op, idx, values, pending, d);
                  }).catch(() => { submit_report(frm, op, idx, values, pending, d); });
                } else {
                  // no restriction configured
                  submit_report(frm, op, idx, values, pending, d);
                }
              } catch (e) {
                submit_report(frm, op, idx, values, pending, d);
              }
            },
            error: function() {
              // if server helper fails, be permissive but log at client
              submit_report(frm, op, idx, values, pending, d);
            }
          });
        } else {
          // workstation not set -> allow reporting but server may reject creating Job Card
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
