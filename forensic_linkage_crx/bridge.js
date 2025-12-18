chrome.runtime.onMessage.addListener((message) => {
  if (!message || message.type !== "time_sync") return;
  const payload = { type: "BILL_EXTRA_TIME_SYNC" };
  if (message.start_time) {
    payload.start_time = message.start_time;
    payload.end_time = message.end_time || message.start_time;
  } else if (message.time) {
    payload.time = message.time;
  }
  window.postMessage(payload, "*");
});

