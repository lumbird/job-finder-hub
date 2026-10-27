// ==UserScript==
// @name         Job Finder Hub: Universal Credit helper
// @namespace    job-finder-hub
// @version      0.1.0
// @description  Bulk delete and bulk add jobs on the Universal Credit work search page. Runs only when you click its buttons.
// @match        https://www.universal-credit.service.gov.uk/work-search*
// @grant        none
// ==/UserScript==

// What it does, all inside your own signed-in tab, one job at a time with a pause between:
// - Job list: a tick box on each job, "Select older than N days", and "Delete selected".
//   Deleting does exactly what the Delete button on each job's Update page does.
// - Add a job page: paste a CSV exported from Job Finder Hub and add every row as "Applied".
// It always asks before changing anything and stops at the first problem.

(() => {
  "use strict";

  const PAUSE_MS = 1500;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const path = location.pathname.replace(/\/+$/, "");

  const panel = (title) => {
    const box = document.createElement("div");
    box.style.cssText =
      "border:3px solid #1d70b8;padding:12px 15px;margin:0 0 20px;background:#f3f8fc;font-family:inherit";
    box.innerHTML = `<h2 class="govuk-heading-s" style="margin:0 0 8px">${title}</h2>`;
    return box;
  };
  const button = (label, secondary = false) => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = label;
    b.className = "govuk-button" + (secondary ? " govuk-button--secondary" : "");
    b.style.marginRight = "10px";
    b.style.marginBottom = "8px";
    return b;
  };
  const status = () => {
    const p = document.createElement("p");
    p.className = "govuk-body";
    p.setAttribute("role", "status");
    p.style.margin = "4px 0 0";
    return p;
  };
  const parse = (html) => new DOMParser().parseFromString(html, "text/html");
  const fieldErrors = (doc) =>
    [...doc.querySelectorAll("[data-validation-error]")].map((e) => e.textContent.trim()).filter(Boolean);

  // ---------- job list: bulk delete ----------

  const MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"];
  const updatedOn = (item) => {
    const m = (item.querySelector(".job-list__item-date")?.textContent || "").match(/(\d{1,2}) (\w+) (\d{4})/);
    if (!m) return null;
    const month = MONTHS.indexOf(m[2].toLowerCase());
    return month < 0 ? null : new Date(+m[3], month, +m[1]);
  };

  async function deleteJob(id) {
    const page = await fetch(`/work-search/job/update/${id}`, { credentials: "same-origin" });
    if (!page.ok) throw new Error(`couldn't open the job (error ${page.status})`);
    const form = parse(await page.text()).querySelector(`form[action="/work-search/job/delete/${id}"]`);
    const token = form?.querySelector('input[name="csrfToken"]')?.value;
    if (!token) throw new Error("couldn't find the job's Delete button. The page may have changed");
    const body = new URLSearchParams({ csrfToken: token, delete: "" });
    const r = await fetch(`/work-search/job/delete/${id}`, { method: "POST", body, credentials: "same-origin" });
    if (!r.ok) throw new Error(`the site replied with error ${r.status}`);
  }

  function setUpList() {
    const items = [...document.querySelectorAll("li.job-list__item[id^='job-container-']")];
    if (!items.length) return;
    const boxes = items.map((item) => {
      const id = item.id.replace("job-container-", "");
      const title = item.querySelector(".job-list__item-heading")?.textContent.trim() || id;
      const label = document.createElement("label");
      label.className = "govuk-body-s";
      label.style.cssText = "display:inline-flex;gap:6px;align-items:center;margin-bottom:6px;cursor:pointer";
      const tick = document.createElement("input");
      tick.type = "checkbox";
      tick.style.cssText = "width:22px;height:22px";
      tick.dataset.id = id;
      tick.dataset.title = title;
      label.append(tick, "Select");
      item.prepend(label);
      return { tick, item };
    });

    const box = panel("Job Finder Hub helper");
    const days = document.createElement("input");
    days.type = "number";
    days.min = "0";
    days.value = "30";
    days.className = "govuk-input govuk-input--width-3";
    days.style.marginRight = "8px";
    const older = button("Select older than these days", true);
    const all = button("Select all on this page", true);
    const none = button("Clear", true);
    const del = button("Delete selected (0)");
    del.classList.add("govuk-button--warning");
    const out = status();
    const daysLabel = document.createElement("span");
    daysLabel.className = "govuk-body";
    daysLabel.textContent = " days ";
    box.append(days, daysLabel, older, all, none, document.createElement("br"), del, out);
    const list = document.querySelector("ul.job-list");
    list.parentElement.insertBefore(box, list);

    const chosen = () => boxes.filter((b) => b.tick.checked);
    const recount = () => (del.textContent = `Delete selected (${chosen().length})`);
    boxes.forEach((b) => b.tick.addEventListener("change", recount));
    all.onclick = () => (boxes.forEach((b) => (b.tick.checked = true)), recount());
    none.onclick = () => (boxes.forEach((b) => (b.tick.checked = false)), recount());
    older.onclick = () => {
      const cutoff = Date.now() - Number(days.value || 0) * 86400000;
      boxes.forEach((b) => {
        const when = updatedOn(b.item);
        b.tick.checked = !!when && when.getTime() < cutoff;
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

  // ---------- add a job: bulk add from a Job Finder Hub export ----------

  // A small CSV reader: quoted fields, commas and new lines inside quotes.
  function readCsv(text) {
    const rows = [];
    let row = [], field = "", quoted = false;
    text = text.replace(/^﻿/, "");
    for (let i = 0; i < text.length; i++) {
      const c = text[i];
      if (quoted) {
        if (c === '"' && text[i + 1] === '"') (field += '"'), i++;
        else if (c === '"') quoted = false;
        else field += c;
      } else if (c === '"') quoted = true;
      else if (c === ",") row.push(field), (field = "");
      else if (c === "\n" || c === "\r") {
        if (c === "\r" && text[i + 1] === "\n") i++;
        row.push(field), rows.push(row), (row = []), (field = "");
      } else field += c;
    }
    if (field || row.length) row.push(field), rows.push(row);
    return rows.filter((r) => r.some((v) => v.trim()));
  }

  function jobsFrom(text) {
    const rows = readCsv(text);
    const head = (rows.shift() || []).map((h) => h.trim().toLowerCase());
    const col = (name) => head.indexOf(name);
    const t = col("job title"), d = col("date applied"), c = col("company");
    if (t < 0 || d < 0) throw new Error('paste a CSV from Job Finder Hub. It needs "Job title" and "Date applied" columns');
    return rows.map((r) => {
      const [y, m, day] = (r[d] || "").trim().split("-");
      if (!y || !m || !day) throw new Error(`"${r[t]}" has no date applied`);
      return { title: r[t].trim(), employer: c >= 0 ? (r[c] || "").trim() : "", y, m, day };
    });
  }

  async function addJob(job) {
    const page = await fetch("/work-search/job", { credentials: "same-origin" });
    if (!page.ok) throw new Error(`couldn't open Add a job (error ${page.status})`);
    const token = parse(await page.text()).querySelector('form[data-job-form] input[name="csrfToken"]')?.value;
    if (!token) throw new Error("couldn't find the Add a job form. The page may have changed");
    const body = new URLSearchParams({
      csrfToken: token,
      jobTitle: job.title,
      employer: job.employer,
      jobStatus: "APPLIED",
      "deadlineDate.day": "",
      "deadlineDate.month": "",
      "deadlineDate.year": "",
      "applicationDate.day": job.day,
      "applicationDate.month": job.m,
      "applicationDate.year": job.y,
      "interviewDate.day": "",
      "interviewDate.month": "",
      "interviewDate.year": "",
      notes: "",
      submit: "Save",
    });
    const r = await fetch("/work-search/job", { method: "POST", body, credentials: "same-origin" });
    if (!r.ok) throw new Error(`the site replied with error ${r.status}`);
    const errors = fieldErrors(parse(await r.text()));
    if (errors.length) throw new Error(errors.join(" "));
  }

  function setUpAdd() {
    const form = document.querySelector("form[data-job-form]");
    if (!form) return;
    const box = panel("Job Finder Hub helper: add many jobs");
    const help = document.createElement("p");
    help.className = "govuk-body-s";
    help.textContent =
      'Paste a CSV from Job Finder Hub (Reporting → Export). Each row is added as "Applied" with its date. ' +
      'If Universal Credit says "Employer or agency" is needed, use the Full list CSV, which has the company.';
    const area = document.createElement("textarea");
    area.className = "govuk-textarea";
    area.rows = 6;
    area.placeholder = "Job title,Date applied,Applied\nJunior Developer,2026-10-27,Yes";
    const go = button("Add these jobs");
    const out = status();
    box.append(help, area, go, out);
    form.parentElement.insertBefore(box, form);

    go.onclick = async () => {
      let jobs;
      try {
        jobs = jobsFrom(area.value);
      } catch (e) {
        return (out.textContent = `Can't read that: ${e.message}.`);
      }
      if (!jobs.length) return (out.textContent = "No jobs in that text.");
      const list = jobs.map((j) => `• ${j.title}${j.employer ? " – " + j.employer : ""} (${j.day}/${j.m}/${j.y})`).join("\n");
      if (!confirm(`Add these ${jobs.length} job(s) to Universal Credit as Applied?\n\n${list}`)) return;
      go.disabled = true;
      let done = 0;
      for (const job of jobs) {
        out.textContent = `Adding ${done + 1} of ${jobs.length}: ${job.title}…`;
        try {
          await addJob(job);
        } catch (e) {
          out.textContent = `Stopped after ${done} added. "${job.title}": ${e.message}`;
          area.value = area.value.split(/\r?\n/).slice(0, 1).concat(area.value.split(/\r?\n/).slice(1 + done)).join("\n");
          go.disabled = false;
          return;
        }
        done += 1;
        await sleep(PAUSE_MS);
      }
      out.textContent = `Added ${done}. Opening your job list…`;
      setTimeout(() => (location.href = "/work-search"), 1200);
    };
  }

  if (path === "/work-search") setUpList();
  if (path === "/work-search/job") setUpAdd();
})();
