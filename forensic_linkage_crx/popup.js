const statusEl = document.getElementById("status");
const bindBtn = document.getElementById("bind");
const unbindBtn = document.getElementById("unbind");

const setStatus = (text) => {
  statusEl.textContent = text;
};

const getActiveTab = async () => {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  return tabs && tabs[0] ? tabs[0] : null;
};

const injectBridge = async (tabId) => {
  await chrome.scripting.executeScript({
    target: { tabId },
    files: ["bridge.js"]
  });
};

const refresh = async () => {
  const data = await chrome.storage.local.get(["boundTabId"]);
  if (data && data.boundTabId) {
    setStatus(`已绑定标签页: ${data.boundTabId}`);
  } else {
    setStatus("未绑定");
  }
};

bindBtn.addEventListener("click", async () => {
  const tab = await getActiveTab();
  if (!tab || !tab.id) {
    setStatus("无法获取当前标签页");
    return;
  }
  await chrome.storage.local.set({ boundTabId: tab.id });
  try {
    await injectBridge(tab.id);
    setStatus(`已绑定标签页: ${tab.id}`);
  } catch (e) {
    setStatus("注入失败：请确认当前页允许扩展访问");
  }
});

unbindBtn.addEventListener("click", async () => {
  await chrome.storage.local.remove(["boundTabId"]);
  setStatus("未绑定");
});

refresh();

