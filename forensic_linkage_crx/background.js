const getBoundTabId = async () => {
  const data = await chrome.storage.local.get(["boundTabId"]);
  return data && data.boundTabId ? data.boundTabId : null;
};

const clearBoundTabId = async () => {
  await chrome.storage.local.remove(["boundTabId"]);
};

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || message.type !== "time_sync") return;
  (async () => {
    const boundTabId = await getBoundTabId();
    if (!boundTabId) {
      sendResponse({ ok: false, error: "not_bound" });
      return;
    }
    try {
      await chrome.tabs.sendMessage(boundTabId, message);
      sendResponse({ ok: true });
    } catch (e) {
      await clearBoundTabId();
      sendResponse({ ok: false, error: "send_failed" });
    }
  })();
  return true;
});

