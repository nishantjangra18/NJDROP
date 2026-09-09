'use strict';

/**
 * NJDROP — Video Downloader
 * Single-page app flow: platform select -> url input -> processing -> complete
 *
 * Talks to the FastAPI backend at /api/download, which uses yt-dlp + FFmpeg
 * to fetch the requested video and streams the finished file back as a
 * Blob. The browser download is triggered automatically once it arrives.
 * Intended only for content the requesting user owns or is authorized to
 * download.
 */

const API_ENDPOINT = '/api/download';

(() => {
  const screens = {
    select: document.getElementById('screen-select'),
    input: document.getElementById('screen-input'),
    processing: document.getElementById('screen-processing'),
    complete: document.getElementById('screen-complete'),
  };

  const state = {
    platform: null, // 'youtube' | 'instagram'
  };

  const platformConfig = {
    youtube: {
      label: 'YouTube Downloader',
      sub: 'Paste a link below to fetch your video',
      placeholder: 'Paste your YouTube video link here…',
      urlPattern: /(youtube\.com|youtu\.be)/i,
      iconHTML: `<svg viewBox="0 0 28 20" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M27.4 3.12A3.44 3.44 0 0024.98.68C22.78 0 14 0 14 0S5.22 0 3.02.68A3.44 3.44 0 00.6 3.12 36.4 36.4 0 000 10a36.4 36.4 0 00.6 6.88 3.44 3.44 0 002.42 2.44C5.22 20 14 20 14 20s8.78 0 10.98-.68a3.44 3.44 0 002.42-2.44A36.4 36.4 0 0028 10a36.4 36.4 0 00-.6-6.88z" fill="#FF2D55"/>
        <path d="M11.2 14.28L18.48 10 11.2 5.72v8.56z" fill="#0A0A0F"/>
      </svg>`,
    },
    instagram: {
      label: 'Instagram Reel Downloader',
      sub: 'Paste a Reel link below to fetch your video',
      placeholder: 'Paste your Instagram Reel link here…',
      urlPattern: /(instagram\.com)/i,
      iconHTML: `<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
        <rect x="1.5" y="1.5" width="21" height="21" rx="6.5" fill="url(#igGradInput)"/>
        <rect x="6.2" y="6.2" width="11.6" height="11.6" rx="4" stroke="#0A0A0F" stroke-width="1.6"/>
        <circle cx="12" cy="12" r="3.1" stroke="#0A0A0F" stroke-width="1.6"/>
        <circle cx="17.15" cy="6.85" r="1.15" fill="#0A0A0F"/>
        <defs>
          <linearGradient id="igGradInput" x1="1.5" y1="22.5" x2="22.5" y2="1.5" gradientUnits="userSpaceOnUse">
            <stop stop-color="#FFC85C"/>
            <stop offset="0.35" stop-color="#FF5C7A"/>
            <stop offset="0.68" stop-color="#D62EC2"/>
            <stop offset="1" stop-color="#8B5CFF"/>
          </linearGradient>
        </defs>
      </svg>`,
    },
  };

  const processingMessages = [
    'Analyzing link…',
    'Preparing your video…',
    'Starting download…',
  ];

  // ---- Screen transition helper ----
  function showScreen(name) {
    Object.entries(screens).forEach(([key, el]) => {
      const isActive = key === name;
      el.setAttribute('data-active', String(isActive));
      el.classList.remove('screen-enter');
    });
    // Trigger enter animation on the newly active screen
    requestAnimationFrame(() => {
      screens[name].classList.add('screen-enter');
    });
  }

  // ---- Step 1: Platform selection ----
  function initPlatformCards() {
    document.querySelectorAll('.platform-card').forEach((card) => {
      card.addEventListener('click', () => {
        const platform = card.dataset.platform;
        selectPlatform(platform);
      });
    });
  }

  function selectPlatform(platform) {
    state.platform = platform;
    const config = platformConfig[platform];

    document.getElementById('input-title').textContent = config.label;
    document.getElementById('input-sub').textContent = config.sub;
    document.getElementById('url-input').placeholder = config.placeholder;
    document.getElementById('input-icon').innerHTML = config.iconHTML;
    document.getElementById('url-input').value = '';
    hideError();

    showScreen('input');
  }

  // ---- Back button ----
  function initBackButton() {
    document.getElementById('back-btn').addEventListener('click', () => {
      showScreen('select');
    });
  }

  // ---- Step 2: Form submit -> processing ----
  function initForm() {
    const form = document.getElementById('download-form');
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      const input = document.getElementById('url-input');
      const url = input.value.trim();
      if (!url) return;

      hideError();
      startProcessing(url);
    });
  }

  // ---- Step 3: Processing animation (trickles while the real request runs) ----
  let progressTimer = null;

  function startProcessing(url) {
    showScreen('processing');

    const statusEl = document.getElementById('processing-status');
    const fillEl = document.getElementById('progress-fill');
    const percentEl = document.getElementById('progress-percent');

    fillEl.style.width = '0%';
    percentEl.textContent = '0%';
    statusEl.textContent = processingMessages[0];

    let progress = 0;
    let messageIndex = 0;

    // Trickles up to ~90% while we wait on the backend, since we don't know
    // real completion time in advance. The final jump to 100% happens once
    // the response actually comes back (see settleProgress()).
    progressTimer = setInterval(() => {
      const remaining = 90 - progress;
      const increment = Math.max(remaining * 0.06, 0.3);
      progress = Math.min(progress + increment, 90);

      fillEl.style.width = `${progress}%`;
      percentEl.textContent = `${Math.round(progress)}%`;

      const expectedMessageIndex = Math.min(
        Math.floor((progress / 90) * (processingMessages.length - 1)),
        processingMessages.length - 2
      );

      if (expectedMessageIndex !== messageIndex) {
        messageIndex = expectedMessageIndex;
        statusEl.style.opacity = '0';
        setTimeout(() => {
          statusEl.textContent = processingMessages[messageIndex];
          statusEl.style.opacity = '1';
        }, 180);
      }
    }, 260);

    runDownload(state.platform, url);
  }

  function settleProgress(onDone) {
    clearInterval(progressTimer);
    const statusEl = document.getElementById('processing-status');
    const fillEl = document.getElementById('progress-fill');
    const percentEl = document.getElementById('progress-percent');

    statusEl.style.opacity = '0';
    setTimeout(() => {
      statusEl.textContent = processingMessages[processingMessages.length - 1];
      statusEl.style.opacity = '1';
    }, 180);

    fillEl.style.width = '100%';
    percentEl.textContent = '100%';

    setTimeout(onDone, 450);
  }

  // ---- Real backend call ----
  async function runDownload(platform, url) {
    try {
      const response = await fetch(API_ENDPOINT, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ platform, url }),
      });

      if (!response.ok) {
        let message = 'Something went wrong while processing this video.';
        try {
          const errorBody = await response.json();
          if (errorBody && errorBody.detail) message = errorBody.detail;
        } catch (_) {
          // response wasn't JSON — keep the generic message
        }
        throw new Error(message);
      }

      const blob = await response.blob();
      const filename = extractFilename(response.headers.get('Content-Disposition')) || `njdrop-${platform}-download.mp4`;

      clearInterval(progressTimer);
      settleProgress(() => {
        triggerBrowserDownload(URL.createObjectURL(blob), filename);
        showScreen('complete');
      });
    } catch (err) {
      clearInterval(progressTimer);
      console.error('Download failed:', err);
      showScreen('input');
      showError(err.message || 'Something went wrong. Please check the link and try again.');
    }
  }

  function extractFilename(contentDisposition) {
    if (!contentDisposition) return null;
    const match = /filename="?([^";]+)"?/i.exec(contentDisposition);
    return match ? match[1] : null;
  }

  // ---- Triggers the browser's native file download ----
  function triggerBrowserDownload(fileUrl, filename) {
    const a = document.createElement('a');
    a.href = fileUrl;
    a.download = filename || '';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(fileUrl), 4000);
  }

  // ---- Error display (input screen) ----
  function showError(message) {
    const errorEl = document.getElementById('error-message');
    errorEl.textContent = message;
    errorEl.hidden = false;
  }

  function hideError() {
    const errorEl = document.getElementById('error-message');
    errorEl.hidden = true;
    errorEl.textContent = '';
  }

  // ---- Restart flow ----
  function initRestartButton() {
    document.getElementById('restart-btn').addEventListener('click', () => {
      state.platform = null;
      showScreen('select');
    });
  }

  // ---- Init ----
  function init() {
    initPlatformCards();
    initBackButton();
    initForm();
    initRestartButton();
    showScreen('select');
  }

  document.addEventListener('DOMContentLoaded', init);

  // ---- PWA: register service worker for the app shell ----
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('/sw.js').catch((err) => {
        console.warn('Service worker registration failed:', err);
      });
    });
  }
})();
