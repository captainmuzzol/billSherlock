const pad2 = (n) => String(n).padStart(2, "0");

const showToast = (text, durationMs = 1500) => {
  try {
    const id = "__bill_extra_linkage_toast";
    let el = document.getElementById(id);
    if (!el) {
      el = document.createElement("div");
      el.id = id;
      el.style.position = "fixed";
      el.style.right = "14px";
      el.style.bottom = "14px";
      el.style.zIndex = "2147483647";
      el.style.background = "rgba(15, 23, 42, 0.92)";
      el.style.color = "#fff";
      el.style.padding = "10px 12px";
      el.style.borderRadius = "12px";
      el.style.fontSize = "12px";
      el.style.lineHeight = "1.4";
      el.style.maxWidth = "260px";
      el.style.boxShadow = "0 10px 30px rgba(0,0,0,0.25)";
      document.documentElement.appendChild(el);
    }
    el.textContent = text;
    el.style.display = "block";
    clearTimeout(showToast._t);
    showToast._t = setTimeout(() => {
      try { el.style.display = "none"; } catch (_) { }
    }, durationMs);
  } catch (_) { }
};

const showReportBannerOnce = () => {
  try {
    if (window.top !== window) return;
    const id = "__bill_extra_linkage_banner";
    if (document.getElementById(id)) return;
    if (sessionStorage.getItem(id) === "1") return;

    const title = String(document.title || "");
    const bodyText = String(document.body ? (document.body.innerText || "") : "");
    const fingerprint = `${title}\n${bodyText.slice(0, 500)}`;
    const isForensic =
      /取证|分析|手机|采分|取证分析报告|电子数据|聊天记录/.test(fingerprint);
    if (!isForensic) return;

    const wrap = document.createElement("div");
    wrap.id = id;
    wrap.style.position = "fixed";
    wrap.style.left = "0";
    wrap.style.right = "0";
    wrap.style.top = "0";
    wrap.style.zIndex = "2147483647";
    wrap.style.background = "rgba(79, 70, 229, 0.95)";
    wrap.style.color = "#fff";
    wrap.style.padding = "12px 14px";
    wrap.style.display = "flex";
    wrap.style.alignItems = "center";
    wrap.style.gap = "12px";
    wrap.style.boxShadow = "0 10px 30px rgba(0,0,0,0.25)";

    const text = document.createElement("div");
    text.style.flex = "1";
    text.style.fontSize = "13px";
    text.style.lineHeight = "1.35";
    text.innerHTML =
      "<div style='font-weight:900;font-size:15px;margin-bottom:2px'>账单神探已启动</div>" +
      "<div style='opacity:0.97'>请打开账单神探账单明细，点击时间可以自动跳转到具体时间，点击头像可以跳转时间段</div>";

    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = "知道了";
    btn.style.border = "1px solid rgba(255,255,255,0.55)";
    btn.style.background = "rgba(255,255,255,0.15)";
    btn.style.color = "#fff";
    btn.style.padding = "7px 10px";
    btn.style.borderRadius = "10px";
    btn.style.cursor = "pointer";
    btn.style.fontWeight = "700";
    btn.onclick = () => {
      try { wrap.remove(); } catch (_) { }
      try { sessionStorage.setItem(id, "1"); } catch (_) { }
    };

    wrap.appendChild(text);
    wrap.appendChild(btn);
    document.documentElement.appendChild(wrap);

    setTimeout(() => {
      try {
        if (wrap && wrap.parentNode) wrap.remove();
        sessionStorage.setItem(id, "1");
      } catch (_) { }
    }, 12000);
  } catch (_) { }
};

const normalize = (y, mo, d, h, mi, s) => {
  const yy = String(y);
  const mm = pad2(mo);
  const dd = pad2(d);
  const hh = pad2(h);
  const mii = pad2(mi);
  const ss = pad2(s == null ? 0 : s);
  return `${yy}-${mm}-${dd} ${hh}:${mii}:${ss}`;
};

const extractTimes = (text) => {
  if (!text) return [];
  const raw = String(text);
  const out = [];
  let yearHint = null;
  const yh1 = /(\d{4})[\/-]\d{1,2}[\/-]\d{1,2}/.exec(raw);
  const yh2 = /(\d{4})年\d{1,2}月\d{1,2}日/.exec(raw);
  if (yh1) yearHint = Number(yh1[1]);
  else if (yh2) yearHint = Number(yh2[1]);
  if (!yearHint || !Number.isFinite(yearHint)) yearHint = new Date().getFullYear();

  const r1 = /(\d{4})[\/-](\d{1,2})[\/-](\d{1,2})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?/g;
  let m1;
  while ((m1 = r1.exec(raw))) {
    out.push(normalize(m1[1], m1[2], m1[3], m1[4], m1[5], m1[6]));
  }

  const r2 = /(\d{4})年(\d{1,2})月(\d{1,2})日\s+(\d{1,2}):(\d{2})(?::(\d{2}))?/g;
  let m2;
  while ((m2 = r2.exec(raw))) {
    out.push(normalize(m2[1], m2[2], m2[3], m2[4], m2[5], m2[6]));
  }

  const r3 = /(?:^|[^\d])(\d{1,2})[\/-](\d{1,2})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?(?:$|[^\d])/g;
  let m3;
  while ((m3 = r3.exec(raw))) {
    out.push(normalize(yearHint, m3[1], m3[2], m3[3], m3[4], m3[5]));
  }

  const r4 = /(?:^|[^\d])(\d{1,2})月(\d{1,2})日\s+(\d{1,2}):(\d{2})(?::(\d{2}))?(?:$|[^\d])/g;
  let m4;
  while ((m4 = r4.exec(raw))) {
    out.push(normalize(yearHint, m4[1], m4[2], m4[3], m4[4], m4[5]));
  }

  return out;
};

const toMs = (normalizedTime) => {
  const s = String(normalizedTime || "");
  const m = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})$/.exec(s);
  if (!m) return null;
  const y = Number(m[1]);
  const mo = Number(m[2]) - 1;
  const d = Number(m[3]);
  const h = Number(m[4]);
  const mi = Number(m[5]);
  const sec = Number(m[6]);
  const dt = new Date(y, mo, d, h, mi, sec, 0);
  const t = dt.getTime();
  return Number.isFinite(t) ? t : null;
};

const pickRange = (times) => {
  const unique = [];
  const seen = new Set();
  for (const t of times || []) {
    if (!t) continue;
    const key = String(t);
    if (seen.has(key)) continue;
    seen.add(key);
    unique.push(key);
  }
  if (unique.length === 0) return null;
  if (unique.length === 1) return { start: unique[0], end: unique[0] };

  const scored = unique
    .map((t) => ({ t, ms: toMs(t) }))
    .filter((x) => x.ms != null)
    .sort((a, b) => a.ms - b.ms);

  if (scored.length >= 2) {
    return { start: scored[0].t, end: scored[scored.length - 1].t };
  }
  unique.sort();
  return { start: unique[0], end: unique[unique.length - 1] };
};

const trySend = (startTime, endTime) => {
  if (!startTime) return;
  const payload = {
    type: "time_sync",
    start_time: startTime,
    end_time: endTime || startTime
  };
  try {
    chrome.runtime.sendMessage(payload, (resp) => {
      const err = chrome.runtime.lastError;
      if (err) {
        showToast("同步失败：扩展无法发送消息");
        return;
      }
      if (!resp || resp.ok !== true) {
        showToast("同步失败：请先绑定账单神探页面");
        return;
      }
      if (payload.end_time && payload.end_time !== payload.start_time) {
        showToast(`已同步区间：${payload.start_time} 至 ${payload.end_time}`, 1800);
      } else {
        showToast(`已同步：${payload.start_time}`, 1800);
      }
    });
  } catch (_) { }
};

const getCandidateElements = (target) => {
  const candidates = [];
  let node = target;
  for (let i = 0; i < 16 && node; i++) {
    if (node.nodeType === 1) candidates.push(node);
    node = node.parentElement;
  }
  return candidates;
};

const isAvatarElement = (el) => {
  if (!el || el.nodeType !== 1) return false;
  const tag = String(el.tagName || "").toLowerCase();
  if (tag === "img") return true;
  const cls = String(el.className || "");
  if (/avatar|portrait|head|photo/i.test(cls)) return true;
  const alt = String(el.getAttribute ? (el.getAttribute("alt") || "") : "");
  if (/头像|avatar/i.test(alt)) return true;
  const aria = String(el.getAttribute ? (el.getAttribute("aria-label") || "") : "");
  if (/头像|avatar/i.test(aria)) return true;
  return false;
};

document.addEventListener(
  "click",
  (e) => {
    const target = e && e.target ? e.target : null;
    if (!target) return;

    const candidates = getCandidateElements(target);
    const avatarClick = candidates.some(isAvatarElement);
    let chosen = null;

    if (avatarClick) {
      const hardContainers = [
        target.closest ? target.closest(".m-message") : null,
        target.closest ? target.closest(".contentitem") : null,
        target.closest ? target.closest("tr") : null,
      ].filter(Boolean);

      for (const el of hardContainers) {
        const text = el.innerText || el.textContent || "";
        if (!text) continue;
        if (text.length > 120000) continue;
        const times = extractTimes(text);
        if (times.length >= 2) {
          chosen = times;
          break;
        }
      }
    }

    for (const el of candidates) {
      const text = el.innerText || el.textContent || "";
      if (!text) continue;
      if (text.length > (avatarClick ? 120000 : 25000)) continue;
      const times = extractTimes(text);

      if (avatarClick) {
        if (!chosen || times.length > chosen.length) {
          chosen = times;
        }
        continue;
      }

      if (times.length >= 1) {
        chosen = times;
        break;
      }
    }

    if (!chosen || chosen.length === 0) return;
    const range = pickRange(chosen);
    if (!range) return;
    trySend(range.start, range.end);
  },
  true
);

showReportBannerOnce();
