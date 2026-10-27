// ==UserScript==
// @name         Job Finder Hub: Universal Credit helper
// @namespace    job-finder-hub
// @version      0.2.0
// @description  Bulk delete, bulk add (from CSV files, checked in a table first) and CSV export on the Universal Credit work search pages. Runs only when you click its buttons.
// @match        https://www.universal-credit.service.gov.uk/work-search*
// @author       LumBird
// @license      MIT
// @grant        none
// ==/UserScript==

// Everything happens inside your own signed-in tab, one job at a time with a pause between.
// - Job list: tick boxes, "Select older than N days", "Delete selected", and Export (quick or full details).
// - Deleted jobs: Export every page as CSV.
// - Add a job: import one or more CSV files (or paste) into a table that mirrors the Add a job form,
//   fix any row, then add them all. Each row is sent exactly as the form would send it.
// It always asks before changing anything.

(() => {
  "use strict";

  const PAUSE_MS = 1500; // between changes
  const READ_PAUSE_MS = 400; // between page reads for exports
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const path = location.pathname.replace(/\/+$/, "");

  // The Add a job form's choices, in its order.
  const STATUSES = [
    ["INTERESTED", "Interested"],
    ["APPLIED", "Applied"],
    ["INTERVIEW", "Going for an interview"],
    ["SUCCESSFUL", "Successful"],
    ["UNSUCCESSFUL", "Unsuccessful"],
  ];
  const COLUMNS = ["Job title", "Employer", "Status", "Date applied", "Deadline", "Interview date", "Notes"];

  // ---------- small helpers ----------

  const el = (tag, props = {}, ...kids) => {
    const node = Object.assign(document.createElement(tag), props);
    node.append(...kids.filter((k) => k !== null && k !== undefined));
    return node;
  };
  const panel = (title) => {
    const box = el("div");
    box.style.cssText = "border:3px solid #1d70b8;padding:12px 15px;margin:0 0 20px;background:#f3f8fc";
    box.append(el("h2", { className: "govuk-heading-s", textContent: title, style: "margin:0 0 8px" }));
    return box;
  };
  const button = (label, kind = "") => {
    const b = el("button", { type: "button", textContent: label, className: "govuk-button" + (kind ? " govuk-button--" + kind : "") });
    b.style.cssText = "margin:0 10px 8px 0";
    return b;
  };
  const status = () => el("p", { className: "govuk-body", role: "status", style: "margin:4px 0 0;white-space:pre-line" });
  const parse = (html) => new DOMParser().parseFromString(html, "text/html");
  const fieldErrors = (doc) =>
    [...doc.querySelectorAll("[data-validation-error]")].map((e) => e.textContent.trim()).filter(Boolean);
  const text = (node) => (node?.textContent || "").replace(/\s+/g, " ").trim();

  async function getPage(url) {
    const r = await fetch(url, { credentials: "same-origin" });
    if (!r.ok) throw new Error(`couldn't open ${url} (error ${r.status})`);
    return parse(await r.text());
  }

  // "Title - Employer" → [title, employer], splitting at the last " - ".
  const splitHeading = (h) => {
    const i = h.lastIndexOf(" - ");
    return i < 0 ? [h, ""] : [h.slice(0, i), h.slice(i + 3)];
  };

  const MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"];
  // "2 October 2026" anywhere in the text → "2026-10-02"
  const isoFromWords = (s) => {
    const m = (s || "").match(/(\d{1,2}) (\w+) (\d{4})/);
    const month = m ? MONTHS.indexOf(m[2].toLowerCase()) : -1;
    return month < 0 ? "" : `${m[3]}-${String(month + 1).padStart(2, "0")}-${m[1].padStart(2, "0")}`;
  };
  // "2026-10-02", "2/10/2026" or "02 10 2026" → "2026-10-02"; anything else → ""
  const isoFrom = (s) => {
    s = (s || "").trim();
    let m = s.match(/^(\d{4})-(\d{1,2})-(\d{1,2})/);
    if (m) return `${m[1]}-${m[2].padStart(2, "0")}-${m[3].padStart(2, "0")}`;
    m = s.match(/^(\d{1,2})[\/. ](\d{1,2})[\/. ](\d{4})$/);
    if (m) return `${m[3]}-${m[2].padStart(2, "0")}-${m[1].padStart(2, "0")}`;
    return isoFromWords(s);
  };
  const dayParts = (iso) => {
    const [y, m, d] = iso ? iso.split("-") : ["", "", ""];
    return { day: d || "", month: m || "", year: y || "" };
  };
  const statusCode = (s) => {
    const v = (s || "").trim().toLowerCase();
    if (!v || v === "yes") return "APPLIED";
    const hit = STATUSES.find(([code, label]) => v === code.toLowerCase() || v === label.toLowerCase());
    if (hit) return hit[0];
    if (v.startsWith("interview") || v === "going for an interview") return "INTERVIEW";
    if (["offer", "successful"].includes(v)) return "SUCCESSFUL";
    if (["closed", "rejected", "unsuccessful"].includes(v)) return "UNSUCCESSFUL";
    if (["found", "saved", "interested"].includes(v)) return "INTERESTED";
    return "APPLIED"; // replied, applied, anything else that means it was sent
  };
  const statusLabel = (code) => (STATUSES.find(([c]) => c === code) || [code, code])[1];

  // ---------- CSV ----------

  function readCsv(src) {
    const rows = [];
    let row = [], field = "", quoted = false;
    src = src.replace(/^﻿/, "");
    for (let i = 0; i < src.length; i++) {
      const c = src[i];
      if (quoted) {
        if (c === '"' && src[i + 1] === '"') (field += '"'), i++;
        else if (c === '"') quoted = false;
        else field += c;
      } else if (c === '"') quoted = true;
      else if (c === ",") row.push(field), (field = "");
      else if (c === "\n" || c === "\r") {
        if (c === "\r" && src[i + 1] === "\n") i++;
        row.push(field), rows.push(row), (row = []), (field = "");
      } else field += c;
    }
    if (field || row.length) row.push(field), rows.push(row);
    return rows.filter((r) => r.some((v) => v.trim()));
  }

  const csvCell = (v) => (/[",\n\r]/.test(v) ? `"${String(v).replace(/"/g, '""')}"` : String(v));
  function download(name, rows) {
    const body = "﻿" + rows.map((r) => r.map((v) => csvCell(v ?? "")).join(",")).join("\r\n") + "\r\n";
    const a = el("a", { href: URL.createObjectURL(new Blob([body], { type: "text/csv" })), download: name });
    document.body.append(a);
    a.click();
    a.remove();
  }
  const today = () => new Date().toISOString().slice(0, 10);

  // CSV text → job objects. Reads this helper's exports and Job Finder Hub's exports.
  function jobsFromCsv(src) {
    const rows = readCsv(src);
    const head = (rows.shift() || []).map((h) => h.trim().toLowerCase());
    const col = (...names) => names.map((n) => head.indexOf(n)).find((i) => i >= 0) ?? -1;
    const c = {
      title: col("job title", "title"),
      employer: col("employer", "company", "employer or agency"),
      status: col("status", "applied"),
      applied: col("date applied", "application date", "applied on"),
      deadline: col("deadline", "closing date"),
      interview: col("interview date", "interview"),
      notes: col("notes"),
      heading: col("heading"),
    };
    if (c.title < 0 && c.heading < 0) throw new Error('a CSV needs a "Job title" column');
    const get = (r, i) => (i >= 0 ? (r[i] || "").trim() : "");
    return rows.map((r) => {
      let title = get(r, c.title), employer = get(r, c.employer);
      if (!title && c.heading >= 0) [title, employer] = splitHeading(get(r, c.heading));
      return {
        title,
        employer,
        status: statusCode(get(r, c.status)),
        applied: isoFrom(get(r, c.applied)),
        deadline: isoFrom(get(r, c.deadline)),
        interview: isoFrom(get(r, c.interview)),
        notes: get(r, c.notes),
      };
    });
  }

  // ---------- reading the site ----------

  // Every page of a list. The page links only show a few pages, so work out the count from
  // "Showing 1 – 15 of 256 results", and stop early if a page comes back empty.
  async function allPages(firstDoc, base, onPage = () => {}) {
    const docs = [firstDoc];
    const m = text(firstDoc.querySelector(".pagination__summary")).match(/(\d+)\D+(\d+)\D+of (\d+)/);
    const perPage = m ? Number(m[2]) - Number(m[1]) + 1 : 0;
    const last = m && perPage > 0 ? Math.ceil(Number(m[3]) / perPage) : 1;
    for (let p = 2; p <= last; p++) {
      onPage(p, last);
      await sleep(READ_PAUSE_MS);
      const doc = await getPage(`${base}?page=${p}`);
      if (!listItems(doc).length) break;
      docs.push(doc);
    }
    return docs;
  }

  const listItems = (doc) =>
    [...doc.querySelectorAll("li.job-list__item[id^='job-container-']")].map((li) => ({
      id: li.id.replace("job-container-", ""),
      heading: text(li.querySelector(".job-list__item-heading")),
      status: text(li.querySelector(".job-list__status-label")),
      date: isoFromWords(text(li.querySelector(".job-list__item-date"))),
    }));

  // One job's Update page → its exact fields.
  async function jobDetails(id) {
    const doc = await getPage(`/work-search/job/update/${id}`);
    const val = (name) => doc.querySelector(`[name="${name}"]`)?.value?.trim() || "";
    const date = (name) => {
      const [d, m, y] = ["day", "month", "year"].map((p) => val(`${name}.${p}`));
      return y && m && d ? `${y}-${m.padStart(2, "0")}-${d.padStart(2, "0")}` : "";
    };
    const titleEl = doc.querySelector("h2.heading-large");
    return {
      title: text(titleEl),
      employer: text(titleEl?.nextElementSibling),
      status: statusLabel(doc.querySelector('input[name="jobStatus"][checked]')?.value || ""),
      applied: date("applicationDate"),
      deadline: date("deadlineDate"),
      interview: date("interviewDate"),
      notes: doc.querySelector('textarea[name="notes"]')?.value?.trim() || "",
    };
  }

  // ---------- job list: delete and export ----------

  async function deleteJob(id) {
    const doc = await getPage(`/work-search/job/update/${id}`);
    const form = doc.querySelector(`form[action="/work-search/job/delete/${id}"]`);
    const token = form?.querySelector('input[name="csrfToken"]')?.value;
    if (!token) throw new Error("couldn't find the job's Delete button. The page may have changed");
    const body = new URLSearchParams({ csrfToken: token, delete: "" });
    const r = await fetch(`/work-search/job/delete/${id}`, { method: "POST", body, credentials: "same-origin" });
    if (!r.ok) throw new Error(`the site replied with error ${r.status}`);
  }

  function setUpList() {
    const list = document.querySelector("ul.job-list");
    const items = [...document.querySelectorAll("li.job-list__item[id^='job-container-']")];
    const box = panel("Job Finder Hub helper");
    const out = status();

    // export
    const quick = button("Export all jobs (CSV)", "secondary");
    const full = button("Export with full details (slower)", "secondary");
    const exportRow = el("div", {}, quick, full);
    const runExport = async (detailed) => {
      quick.disabled = full.disabled = true;
      try {
        out.textContent = "Reading your job list…";
        const docs = await allPages(document, "/work-search", (p, n) => (out.textContent = `Reading page ${p} of ${n}…`));
        const jobs = docs.flatMap(listItems);
        let rows;
        if (detailed) {
          rows = [COLUMNS];
          for (const [i, j] of jobs.entries()) {
            out.textContent = `Reading job ${i + 1} of ${jobs.length}…`;
            const d = await jobDetails(j.id);
            rows.push([d.title, d.employer, d.status, d.applied, d.deadline, d.interview, d.notes]);
            await sleep(READ_PAUSE_MS);
          }
        } else {
          rows = [["Job title", "Employer", "Status", "Last updated", "Heading"]];
          for (const j of jobs) rows.push([...splitHeading(j.heading), j.status, j.date, j.heading]);
        }
        download(`uc-jobs-${detailed ? "full-" : ""}${today()}.csv`, rows);
        out.textContent = `Exported ${rows.length - 1} job(s).`;
      } catch (e) {
        out.textContent = `Export stopped: ${e.message}.`;
      }
      quick.disabled = full.disabled = false;
    };
    quick.onclick = () => runExport(false);
    full.onclick = () => runExport(true);
    box.append(exportRow);

    if (items.length) {
      const boxes = items.map((item) => {
        const id = item.id.replace("job-container-", "");
        const title = text(item.querySelector(".job-list__item-heading")) || id;
        const tick = el("input", { type: "checkbox", style: "width:22px;height:22px" });
        Object.assign(tick.dataset, { id, title });
        const label = el("label", { className: "govuk-body-s" }, tick, "Select");
        label.style.cssText = "display:inline-flex;gap:6px;align-items:center;margin-bottom:6px;cursor:pointer";
        item.prepend(label);
        return { tick, item };
      });
      const days = el("input", { type: "number", min: "0", value: "30", className: "govuk-input govuk-input--width-3" });
      days.style.marginRight = "8px";
      const older = button("Select older than these days", "secondary");
      const all = button("Select all on this page", "secondary");
      const none = button("Clear", "secondary");
      const del = button("Delete selected (0)", "warning");
      box.append(el("div", {}, days, el("span", { className: "govuk-body", textContent: " days " }), older, all, none), del);

      const chosen = () => boxes.filter((b) => b.tick.checked);
      const recount = () => (del.textContent = `Delete selected (${chosen().length})`);
      boxes.forEach((b) => b.tick.addEventListener("change", recount));
      all.onclick = () => (boxes.forEach((b) => (b.tick.checked = true)), recount());
      none.onclick = () => (boxes.forEach((b) => (b.tick.checked = false)), recount());
      older.onclick = () => {
        const cutoff = Date.now() - Number(days.value || 0) * 86400000;
        boxes.forEach((b) => {
          const iso = isoFromWords(text(b.item.querySelector(".job-list__item-date")));
          b.tick.checked = !!iso && new Date(iso).getTime() < cutoff;
        });
        recount();
      };
      del.onclick = async () => {
        const picked = chosen();
        if (!picked.length) return (out.textContent = "Tick some jobs first.");
        const names = picked.map((b) => "• " + b.tick.dataset.title).join("\n");
        if (!confirm(`Delete these ${picked.length} job(s) from Universal Credit?\n\n${names}\n\nThey move to "Deleted jobs".`)) return;
        del.disabled = true;
        let done = 0;
        for (const b of picked) {
          out.textContent = `Deleting ${done + 1} of ${picked.length}: ${b.tick.dataset.title}…`;
          try {
            await deleteJob(b.tick.dataset.id);
          } catch (e) {
            out.textContent = `Stopped after ${done} deleted. "${b.tick.dataset.title}": ${e.message}.`;
            del.disabled = false;
            return;
          }
          done += 1;
          b.item.style.opacity = "0.35";
          b.tick.checked = false;
          b.tick.disabled = true;
          await sleep(PAUSE_MS);
        }
        out.textContent = `Deleted ${done}. Reloading…`;
        setTimeout(() => location.reload(), 1200);
      };
    }
    box.append(out);
    const anchor = list || document.querySelector("#add-job")?.parentElement;
    if (anchor) anchor.parentElement.insertBefore(box, anchor);
  }

  function setUpDeleted() {
    const list = document.querySelector("ul.job-list");
    if (!list) return;
    const box = panel("Job Finder Hub helper");
    const go = button("Export all deleted jobs (CSV)", "secondary");
    const out = status();
    box.append(go, out);
    list.parentElement.insertBefore(box, list);
    go.onclick = async () => {
      go.disabled = true;
      try {
        out.textContent = "Reading every page…";
        const docs = await allPages(document, "/work-search/deleted", (p, n) => (out.textContent = `Reading page ${p} of ${n}…`));
        const jobs = docs.flatMap(listItems);
        const rows = [["Job title", "Employer", "Deleted on", "Heading"]];
        for (const j of jobs) rows.push([...splitHeading(j.heading), j.date, j.heading]);
        download(`uc-deleted-jobs-${today()}.csv`, rows);
        out.textContent = `Exported ${jobs.length} deleted job(s).`;
      } catch (e) {
        out.textContent = `Export stopped: ${e.message}.`;
      }
      go.disabled = false;
    };
  }

  // ---------- add a job: import into a table, check, then add ----------

  // Send one job exactly as the Add a job form does.
  async function addJob(job) {
    const doc = await getPage("/work-search/job");
    const token = doc.querySelector('form[data-job-form] input[name="csrfToken"]')?.value;
    if (!token) throw new Error("couldn't find the Add a job form. The page may have changed");
    const body = new URLSearchParams({ csrfToken: token, jobTitle: job.title, employer: job.employer, jobStatus: job.status });
    for (const [name, iso] of [["deadlineDate", job.deadline], ["applicationDate", job.applied], ["interviewDate", job.interview]]) {
      const p = dayParts(iso);
      body.append(`${name}.day`, p.day);
      body.append(`${name}.month`, p.month);
      body.append(`${name}.year`, p.year);
    }
    body.append("notes", job.notes);
    body.append("submit", "Save");
    const r = await fetch("/work-search/job", { method: "POST", body, credentials: "same-origin" });
    if (!r.ok) throw new Error(`the site replied with error ${r.status}`);
    const errors = fieldErrors(parse(await r.text()));
    if (errors.length) {
      const err = new Error(errors.join(" "));
      err.fieldProblem = true; // the site said no to this row; the others can still go
      throw err;
    }
  }

  // Checks the site would make, so problems show before anything is sent.
  function problems(job) {
    const p = [];
    if (!job.title) p.push("needs a job title");
    if (job.title.length > 250) p.push("job title is over 250 characters");
    if (job.notes.length > 3000) p.push(`notes are ${job.notes.length} characters; the limit is 3000`);
    if (!job.employer) p.push("employer is empty (the site may ask for it)");
    if (["APPLIED", "INTERVIEW", "SUCCESSFUL", "UNSUCCESSFUL"].includes(job.status) && !job.applied) p.push("needs the date you applied");
    if (job.status === "INTERVIEW" && !job.interview) p.push("needs the interview date");
    return p;
  }

  function setUpAdd() {
    const form = document.querySelector("form[data-job-form]");
    if (!form) return;
    const box = panel("Job Finder Hub helper: add many jobs");
    box.append(
      el("p", {
        className: "govuk-body-s",
        textContent:
          "Import CSV files from Job Finder Hub or from this helper's exports. Each job becomes a row below with the same " +
          "boxes as the form. Fix anything, untick rows you don't want, then add them all.",
      })
    );
    const files = el("input", { type: "file", accept: ".csv,text/csv", multiple: true, className: "govuk-file-upload" });
    const paste = el("textarea", { className: "govuk-textarea", rows: 3, placeholder: "…or paste CSV text here" });
    const load = button("Add pasted text to the table", "secondary");
    const clear = button("Clear the table", "secondary");
    const out = status();

    const table = el("table", { className: "govuk-table" });
    table.style.cssText = "font-size:16px;display:block;overflow-x:auto";
    const headRow = el("tr", {}, ...["Add", ...COLUMNS.slice(0, 2), "Where are you with this job?", "When did you apply?", "Deadline", "Interview", "Notes", ""].map(
      (h) => el("th", { className: "govuk-table__header", textContent: h, scope: "col" })
    ));
    const tbody = el("tbody");
    table.append(el("thead", {}, headRow), tbody);
    const go = button("Add ticked jobs (0)");
    box.append(el("div", {}, files), paste, el("div", {}, load, clear), table, go, out);
    form.parentElement.insertBefore(box, form);

    const cell = (node) => el("td", { className: "govuk-table__cell", style: "vertical-align:top;padding-right:8px" }, node);
    const rows = () => [...tbody.querySelectorAll("tr")];
    const readRow = (tr) => {
      const v = (k) => tr.querySelector(`[data-k="${k}"]`).value.trim();
      return { title: v("title"), employer: v("employer"), status: v("status"), applied: v("applied"), deadline: v("deadline"), interview: v("interview"), notes: v("notes") };
    };
    const ticked = () => rows().filter((tr) => tr.querySelector('[data-k="add"]').checked);
    const refresh = () => {
      let warn = 0;
      for (const tr of rows()) {
        const p = problems(readRow(tr));
        const note = tr.querySelector(".hub-row-note");
        if (!tr.dataset.error) note.textContent = p.join("; ");
        note.style.color = tr.dataset.error ? "#d4351c" : "#505a5f";
        if (p.length && tr.querySelector('[data-k="add"]').checked) warn += 1;
      }
      go.textContent = `Add ticked jobs (${ticked().length})`;
      return warn;
    };

    const addRow = (job) => {
      const input = (k, type = "text", width = "") => {
        const i = el("input", { type, className: "govuk-input" + width, value: job[k] || "" });
        i.dataset.k = k;
        if (k === "title") i.maxLength = 250;
        return i;
      };
      const select = el("select", { className: "govuk-select" }, ...STATUSES.map(([code, label]) => el("option", { value: code, textContent: label })));
      select.dataset.k = "status";
      select.value = job.status || "APPLIED";
      const add = el("input", { type: "checkbox", checked: true, style: "width:22px;height:22px" });
      add.dataset.k = "add";
      const notes = el("textarea", { className: "govuk-textarea", rows: 1, value: job.notes || "", maxLength: 3000 });
      notes.dataset.k = "notes";
      const remove = button("✕", "secondary");
      remove.title = "Remove this row";
      remove.style.cssText = "margin:0;padding:4px 8px";
      const titleCell = cell(input("title"));
      titleCell.append(el("div", { className: "govuk-body-s hub-row-note", style: "margin:2px 0 0" }));
      const tr = el(
        "tr",
        { className: "govuk-table__row" },
        cell(add),
        titleCell,
        cell(input("employer")),
        cell(select),
        cell(input("applied", "date")),
        cell(input("deadline", "date")),
        cell(input("interview", "date")),
        cell(notes),
        cell(remove)
      );
      tr.addEventListener("input", () => (delete tr.dataset.error, refresh()));
      tr.addEventListener("change", () => (delete tr.dataset.error, refresh()));
      remove.onclick = () => (tr.remove(), refresh());
      tbody.append(tr);
    };

    const take = (src, from) => {
      try {
        const jobs = jobsFromCsv(src);
        jobs.forEach(addRow);
        out.textContent = `Added ${jobs.length} row(s) from ${from}. ${refresh() ? "Some rows need a look (grey notes)." : ""}`;
      } catch (e) {
        out.textContent = `Couldn't read ${from}: ${e.message}.`;
      }
    };
    files.onchange = async () => {
      for (const f of files.files) take(await f.text(), f.name);
      files.value = "";
    };
    load.onclick = () => (take(paste.value, "the pasted text"), (paste.value = ""));
    clear.onclick = () => (tbody.replaceChildren(), refresh(), (out.textContent = ""));

    go.onclick = async () => {
      const picked = ticked();
      if (!picked.length) return (out.textContent = "Nothing ticked to add.");
      const warn = refresh();
      const list = picked.map((tr) => {
        const j = readRow(tr);
        return `• ${j.title}${j.employer ? " – " + j.employer : ""} (${statusLabel(j.status)}${j.applied ? ", " + j.applied : ""})`;
      });
      const extra = warn ? `\n\n${warn} row(s) have notes; the site may refuse them.` : "";
      if (!confirm(`Add these ${picked.length} job(s) to Universal Credit?\n\n${list.join("\n")}${extra}`)) return;
      go.disabled = true;
      let added = 0, refused = 0;
      for (const [i, tr] of picked.entries()) {
        const job = readRow(tr);
        out.textContent = `Adding ${i + 1} of ${picked.length}: ${job.title}…`;
        try {
          await addJob(job);
          added += 1;
          tr.remove();
        } catch (e) {
          if (!e.fieldProblem) {
            out.textContent = `Stopped after ${added} added. "${job.title}": ${e.message}.`;
            go.disabled = false;
            return refresh();
          }
          refused += 1;
          tr.dataset.error = "1";
          tr.querySelector(".hub-row-note").textContent = "Universal Credit said: " + e.message;
        }
        refresh();
        await sleep(PAUSE_MS);
      }
      go.disabled = false;
      out.textContent = refused
        ? `Added ${added}. ${refused} were refused; they're still in the table in red. Fix them and add again.`
        : `Added ${added}. Opening your job list…`;
      if (!refused) setTimeout(() => (location.href = "/work-search"), 1200);
    };
  }

  if (path === "/work-search") setUpList();
  if (path === "/work-search/deleted") setUpDeleted();
  if (path === "/work-search/job") setUpAdd();
})();
