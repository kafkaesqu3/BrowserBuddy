let mediaRecorder;
let recordedChunks = [];
let isRecording = false;

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.action === 'startRecording') {
    startTabRecording(sendResponse);
    return true; // Keep the message channel open for async response
  } else if (message.action === 'stopRecording') {
    stopRecording(sendResponse);
    return true;
  } else if (message.action === 'getRecordingState') {
    sendResponse({ isRecording });
  } else if (message.action === 'takeScreenshot') {
    takeScreenshot(sendResponse);
    return true;
  }
});

async function startTabRecording(sendResponse) {
  try {
    // Get the current active tab
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    
    if (!tab) {
      sendResponse({ success: false, error: 'No active tab found' });
      return;
    }

    // Start capturing the tab's audio and video
    const stream = await chrome.tabCapture.capture({
      audio: true,
      video: true,
      audioConstraints: {
        mandatory: {
          chromeMediaSource: 'tab',
          chromeMediaSourceId: tab.id.toString()
        }
      },
      videoConstraints: {
        mandatory: {
          chromeMediaSource: 'tab',
          chromeMediaSourceId: tab.id.toString(),
          maxWidth: 1920,
          maxHeight: 1080,
          maxFrameRate: 30
        }
      }
    });

    if (!stream) {
      sendResponse({ success: false, error: 'Failed to capture tab' });
      return;
    }

    // Create MediaRecorder to record the stream
    recordedChunks = [];
    mediaRecorder = new MediaRecorder(stream, {
      mimeType: 'video/webm;codecs=vp9,opus'
    });

    mediaRecorder.ondataavailable = (event) => {
      if (event.data.size > 0) {
        recordedChunks.push(event.data);
      }
    };

    mediaRecorder.onstop = () => {
      stream.getTracks().forEach(track => track.stop());
      isRecording = false;
    };

    mediaRecorder.start();
    isRecording = true;

    sendResponse({ success: true });
  } catch (error) {
    console.error('Error starting recording:', error);
    sendResponse({ success: false, error: error.message });
  }
}

function stopRecording(sendResponse) {
  if (mediaRecorder && isRecording) {
    mediaRecorder.stop();
    
    mediaRecorder.onstop = () => {
      // Create blob from recorded chunks
      const blob = new Blob(recordedChunks, { type: 'video/webm' });
      const url = URL.createObjectURL(blob);
      
      sendResponse({ 
        success: true, 
        videoUrl: url,
        blob: blob
      });
    };
  } else {
    sendResponse({ success: false, error: 'No active recording' });
  }
}

async function takeScreenshot(sendResponse) {
  try {
    // Get the current active tab
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    
    if (!tab) {
      sendResponse({ success: false, error: 'No active tab found' });
      return;
    }

    // Capture the visible tab as an image
    const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, {
      format: 'png',
      quality: 100
    });

    // Create filename with timestamp
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
    const filename = `tab-screenshot-${timestamp}.png`;

    // Get the download directory from storage or use default
    const result = await chrome.storage.sync.get(['downloadDirectory']);
    const downloadDirectory = result.downloadDirectory || 'Screenshots';

    // Use Chrome downloads API to save directly to disk
    const downloadId = await chrome.downloads.download({
      url: dataUrl,
      filename: `${downloadDirectory}/${filename}`,
      saveAs: false // Don't show save dialog, save directly
    });

    sendResponse({ 
      success: true, 
      downloadId: downloadId,
      filename: filename,
      directory: downloadDirectory
    });
  } catch (error) {
    console.error('Error taking screenshot:', error);
    sendResponse({ success: false, error: error.message });
  }
}