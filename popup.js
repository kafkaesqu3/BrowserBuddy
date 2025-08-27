document.addEventListener('DOMContentLoaded', function() {
  const screenshotBtn = document.getElementById('screenshotBtn');
  const startBtn = document.getElementById('startBtn');
  const stopBtn = document.getElementById('stopBtn');
  const downloadBtn = document.getElementById('downloadBtn');
  const status = document.getElementById('status');
  const downloadLink = document.getElementById('downloadLink');
  const downloadDirectoryInput = document.getElementById('downloadDirectory');
  
  let videoBlob = null;
  let screenshotBlob = null;

  // Check initial recording state and load settings
  checkRecordingState();
  loadSettings();

  screenshotBtn.addEventListener('click', takeScreenshot);
  startBtn.addEventListener('click', startRecording);
  stopBtn.addEventListener('click', stopRecording);
  downloadBtn.addEventListener('click', downloadVideo);
  downloadDirectoryInput.addEventListener('input', saveSettings);

  function checkRecordingState() {
    chrome.runtime.sendMessage({ action: 'getRecordingState' }, (response) => {
      if (response && response.isRecording) {
        updateUI('recording');
      } else {
        updateUI('stopped');
      }
    });
  }

  function startRecording() {
    updateUI('starting');
    
    chrome.runtime.sendMessage({ action: 'startRecording' }, (response) => {
      if (response.success) {
        updateUI('recording');
        // Notify content script that recording started
        chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
          chrome.tabs.sendMessage(tabs[0].id, { action: 'recordingStarted' });
        });
      } else {
        updateUI('error');
        status.textContent = `Error: ${response.error}`;
      }
    });
  }

  function stopRecording() {
    updateUI('stopping');
    
    chrome.runtime.sendMessage({ action: 'stopRecording' }, (response) => {
      if (response.success) {
        videoBlob = response.blob;
        updateUI('finished');
        // Notify content script that recording stopped
        chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
          chrome.tabs.sendMessage(tabs[0].id, { action: 'recordingStopped' });
        });
      } else {
        updateUI('error');
        status.textContent = `Error: ${response.error}`;
      }
    });
  }

  function loadSettings() {
    chrome.storage.sync.get(['downloadDirectory'], (result) => {
      if (result.downloadDirectory) {
        downloadDirectoryInput.value = result.downloadDirectory;
      }
    });
  }

  function saveSettings() {
    const directory = downloadDirectoryInput.value.trim() || 'Screenshots';
    chrome.storage.sync.set({ downloadDirectory: directory });
  }

  function takeScreenshot() {
    status.textContent = 'Taking screenshot...';
    screenshotBtn.disabled = true;
    
    // Save current directory setting before taking screenshot
    saveSettings();
    
    chrome.runtime.sendMessage({ action: 'takeScreenshot' }, (response) => {
      if (response.success) {
        status.textContent = `Screenshot saved to ${response.directory}/${response.filename}`;
        status.className = 'status stopped';
        
        // Reset status after a short delay
        setTimeout(() => {
          status.textContent = 'Ready to record';
        }, 3000);
      } else {
        status.textContent = `Error: ${response.error}`;
        status.className = 'status recording';
      }
      screenshotBtn.disabled = false;
    });
  }

  function downloadVideo() {
    if (videoBlob) {
      const url = URL.createObjectURL(videoBlob);
      const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
      const filename = `tab-recording-${timestamp}.webm`;
      
      downloadLink.href = url;
      downloadLink.download = filename;
      downloadLink.click();
      
      // Clean up URL after download
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
  }

  function updateUI(state) {
    switch (state) {
      case 'starting':
        status.textContent = 'Starting recording...';
        status.className = 'status';
        startBtn.disabled = true;
        stopBtn.disabled = true;
        downloadBtn.disabled = true;
        break;
        
      case 'recording':
        status.textContent = 'Recording in progress';
        status.className = 'status recording';
        startBtn.disabled = true;
        stopBtn.disabled = false;
        downloadBtn.disabled = true;
        break;
        
      case 'stopping':
        status.textContent = 'Stopping recording...';
        status.className = 'status';
        startBtn.disabled = true;
        stopBtn.disabled = true;
        downloadBtn.disabled = true;
        break;
        
      case 'finished':
        status.textContent = 'Recording finished';
        status.className = 'status stopped';
        startBtn.disabled = false;
        stopBtn.disabled = true;
        downloadBtn.disabled = false;
        break;
        
      case 'stopped':
        status.textContent = 'Ready to record';
        status.className = 'status stopped';
        startBtn.disabled = false;
        stopBtn.disabled = true;
        downloadBtn.disabled = true;
        break;
        
      case 'error':
        status.className = 'status recording';
        startBtn.disabled = false;
        stopBtn.disabled = true;
        downloadBtn.disabled = true;
        break;
    }
  }
});