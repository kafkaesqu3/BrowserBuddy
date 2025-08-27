// Content script to help with tab recording
// This script runs in the context of web pages and can communicate with the background script

// Listen for messages from popup or background script
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.action === 'checkTabReady') {
    // Confirm the tab is ready for recording
    sendResponse({ ready: true, url: window.location.href });
  }
});

// Optional: Add visual indicator when recording is active
let recordingIndicator = null;

function showRecordingIndicator() {
  if (recordingIndicator) return;
  
  recordingIndicator = document.createElement('div');
  recordingIndicator.id = 'screen-recorder-indicator';
  recordingIndicator.innerHTML = '🔴 Recording';
  recordingIndicator.style.cssText = `
    position: fixed;
    top: 10px;
    right: 10px;
    background: rgba(255, 0, 0, 0.8);
    color: white;
    padding: 8px 12px;
    border-radius: 4px;
    font-family: Arial, sans-serif;
    font-size: 14px;
    font-weight: bold;
    z-index: 999999;
    pointer-events: none;
    animation: pulse 1s infinite;
  `;
  
  // Add pulse animation
  const style = document.createElement('style');
  style.textContent = `
    @keyframes pulse {
      0% { opacity: 1; }
      50% { opacity: 0.6; }
      100% { opacity: 1; }
    }
  `;
  document.head.appendChild(style);
  document.body.appendChild(recordingIndicator);
}

function hideRecordingIndicator() {
  if (recordingIndicator) {
    recordingIndicator.remove();
    recordingIndicator = null;
  }
}

// Listen for recording state changes
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.action === 'recordingStarted') {
    showRecordingIndicator();
  } else if (message.action === 'recordingStopped') {
    hideRecordingIndicator();
  }
});