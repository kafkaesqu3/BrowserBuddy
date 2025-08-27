# Chrome Tab Screen Recorder Extension

A Chrome extension that allows you to record your current browser tab with audio and video.

## Features

- Record the current active tab including audio and video
- Visual recording indicator on the page
- Download recordings as WebM files
- Simple and intuitive popup interface

## Installation

1. Open Chrome and navigate to `chrome://extensions/`
2. Enable "Developer mode" in the top right corner
3. Click "Load unpacked" and select this folder
4. The extension icon should appear in your toolbar

## Usage

1. Navigate to the tab you want to record
2. Click the extension icon in your toolbar
3. Click "Start Recording" to begin recording
4. A red recording indicator will appear on the page
5. Click "Stop Recording" when finished
6. Click "Download Video" to save the recording

## Files

- `manifest.json` - Extension configuration
- `popup.html` - Popup interface
- `popup.js` - Popup functionality
- `background.js` - Background script handling recording
- `content.js` - Content script for page interactions

## Permissions

- `activeTab` - Access to current tab
- `tabCapture` - Required for screen recording
- `storage` - Store extension settings

## Technical Details

The extension uses Chrome's `tabCapture` API to record tab audio and video, and the MediaRecorder API to save the recording as a WebM file.

## Browser Compatibility

This extension requires Chrome 88+ for full functionality.