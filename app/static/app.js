(() => {
  "use strict";

  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const state = { mode: "simple", info: null, jobId: null, pollTimer: null };

  class ApiError extends Error {
    constructor(message, code) {
      super(message);
      this.code = code;
    }
  }

  function initLargeText() {
    const toggle = document.getElementById("large-text-toggle");
    if (!toggle) return;
    const enabled = window.localStorage.getItem("yt-large-text") === "true";
    document.body.classList.toggle("large-text", enabled);
    toggle.setAttribute("aria-pressed", String(enabled));
    toggle.addEventListener("click", () => {
      const next = !document.body.classList.contains("large-text");
      document.body.classList.toggle("large-text", next);
      toggle.setAttribute("aria-pressed", String(next));
      window.localStorage.setItem("yt-large-text", String(next));
    });
  }

  async function requestJson(url, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set("Accept", "application/json");
    if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
    if (options.method && options.method !== "GET") headers.set("X-CSRF-Token", csrfToken);
    const response = await fetch(url, { ...options, headers, credentials: "same-origin" });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = payload.detail || payload;
      const message = typeof detail === "string" ? detail : detail.message || "処理に失敗しました";
      if (response.status === 401) {
        window.location.href = `/login?next=${encodeURIComponent(window.location.pathname)}`;
      }
      throw new ApiError(message, detail.code);
    }
    return payload;
  }

  function setAlert(message, type = "error") {
    const alert = document.getElementById("app-alert");
    if (!alert) return;
    alert.textContent = message || "";
    alert.className = `notice notice-${type}`;
    alert.hidden = !message;
  }

  function setButtonBusy(button, busy, busyText) {
    if (!button) return;
    if (busy) {
      button.dataset.defaultLabel = button.textContent;
      button.textContent = busyText;
      button.disabled = true;
    } else {
      button.textContent = button.dataset.defaultLabel || button.textContent;
      button.disabled = false;
    }
  }

  const SAVE_PICKER_MIME_TYPES = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".opus": "audio/ogg",
  };

  function showSavePickerMessage(message, type = "error") {
    if (document.getElementById("app-alert")) {
      setAlert(message, type);
    } else if (message) {
      window.alert(message);
    }
  }

  function savePickerOptions(filename) {
    const suggestedName = filename || "youtube-file";
    const extensionMatch = suggestedName.toLowerCase().match(/\.[a-z0-9]+$/);
    const extension = extensionMatch ? extensionMatch[0] : "";
    const mimeType = SAVE_PICKER_MIME_TYPES[extension];
    const options = { suggestedName };
    if (mimeType) {
      options.types = [{
        description: "メディアファイル",
        accept: { [mimeType]: [extension] },
      }];
    }
    return options;
  }

  function supportsSaveLocationPicker() {
    return typeof window.showSaveFilePicker === "function" ||
      typeof window.showDirectoryPicker === "function";
  }

  async function chooseSaveFileHandle(filename) {
    if (typeof window.showSaveFilePicker === "function") {
      return window.showSaveFilePicker(savePickerOptions(filename));
    }
    if (typeof window.showDirectoryPicker === "function") {
      const directoryHandle = await window.showDirectoryPicker({
        mode: "readwrite",
        startIn: "downloads",
      });
      return directoryHandle.getFileHandle(filename, { create: true });
    }
    throw new Error("このブラウザは保存先の選択に対応していません");
  }

  function startBrowserDownload(button) {
    const url = button.dataset.savePickerUrl;
    if (!url) {
      showSavePickerMessage("保存できるファイルがありません");
      return;
    }
    const link = document.createElement("a");
    link.href = url;
    link.download = button.dataset.savePickerFilename || "youtube-file";
    link.hidden = true;
    document.body.append(link);
    link.click();
    link.remove();
    showSavePickerMessage(
      "このブラウザではサイトから保存先を選べません。Chromeのダウンロード設定で「保存場所を確認」を有効にしてください。",
      "info",
    );
  }

  async function saveWithPicker(button) {
    const url = button.dataset.savePickerUrl;
    const filename = button.dataset.savePickerFilename || "youtube-file";
    if (!url) {
      showSavePickerMessage("保存できるファイルがありません");
      return;
    }
    setButtonBusy(button, true, "保存しています…");
    try {
      // This must run directly from the user's click while transient activation is active.
      const fileHandle = await chooseSaveFileHandle(filename);
      const response = await fetch(url, { credentials: "same-origin" });
      if (!response.ok) {
        if (response.status === 401) {
          window.location.href = `/login?next=${encodeURIComponent(window.location.pathname)}`;
          return;
        }
        throw new Error("ファイルを取得できませんでした");
      }

      const writable = await fileHandle.createWritable();
      try {
        if (response.body?.pipeTo) {
          await response.body.pipeTo(writable);
        } else {
          await writable.write(await response.blob());
          await writable.close();
        }
      } catch (error) {
        await writable.abort().catch(() => {});
        throw error;
      }
      showSavePickerMessage("ファイルを保存しました", "success");
    } catch (error) {
      if (error?.name !== "AbortError") {
        showSavePickerMessage(error.message || "ファイルを保存できませんでした");
      }
    } finally {
      setButtonBusy(button, false);
    }
  }

  function initSavePickerButtons() {
    const buttons = document.querySelectorAll(".save-picker-button");
    const supported = supportsSaveLocationPicker();
    buttons.forEach((button) => {
      button.hidden = false;
      button.addEventListener("click", () => {
        if (supportsSaveLocationPicker()) {
          saveWithPicker(button);
        } else {
          startBrowserDownload(button);
        }
      });
    });
    const help = document.getElementById("save-picker-help");
    if (help) {
      help.textContent = supported
        ? "対応ブラウザでは、保存先のフォルダとファイル名を選べます。"
        : "このChromeではサイトから保存先を選べないため、Chromeのダウンロード設定を使用します。";
      help.hidden = false;
    }
  }

  function populateSelect(select, items, selectedValue) {
    if (!select) return;
    select.replaceChildren();
    items.forEach((item) => {
      const option = document.createElement("option");
      option.value = String(item.value);
      option.textContent = item.label;
      if (item.disabled) option.disabled = true;
      select.append(option);
    });
    if (selectedValue !== undefined && items.some((item) => String(item.value) === String(selectedValue))) {
      select.value = String(selectedValue);
    }
  }

  function selectedTarget() {
    return document.querySelector('input[name="target"]:checked')?.value || "audio";
  }

  function selectedSimpleHeight() {
    const options = state.info?.video_options || [];
    if (!options.length) return 720;
    const capped = options.filter((item) => item.height <= 720);
    return (options.find((item) => item.height === 720) ||
      capped[capped.length - 1] ||
      options[0]).height;
  }

  function renderSimpleNotes() {
    const videoNote = document.getElementById("simple-video-note");
    if (videoNote && state.info) {
      const height = selectedSimpleHeight();
      videoNote.querySelector("span:nth-child(2)").textContent = `MP4・最大${height}p`;
      videoNote.querySelector("span:nth-child(4)").textContent = height <= 720 ? "スマホでも再生しやすい形式" : "この動画で利用できる画質を使用";
    }
  }

  function updateTargetUI() {
    const target = selectedTarget();
    document.querySelectorAll(".choice-card").forEach((card) => {
      const input = card.querySelector('input[name="target"]');
      card.classList.toggle("is-selected", Boolean(input?.checked));
    });
    const videoSettings = document.getElementById("video-settings");
    const audioSettings = document.getElementById("audio-settings");
    const simpleVideo = document.getElementById("simple-video-note");
    const simpleAudio = document.getElementById("simple-audio-note");
    if (videoSettings) videoSettings.hidden = target !== "video";
    if (audioSettings) audioSettings.hidden = target !== "audio";
    if (simpleVideo) simpleVideo.hidden = target !== "video";
    if (simpleAudio) simpleAudio.hidden = target !== "audio";
    const qualityField = document.getElementById("mp3-quality-field");
    if (qualityField) qualityField.hidden = document.getElementById("audio-format")?.value !== "mp3";
    renderSimpleNotes();
  }

  function updateMode(mode) {
    state.mode = mode;
    window.localStorage.setItem("yt-mode", mode);
    document.querySelectorAll("[data-mode]").forEach((button) => {
      const active = button.dataset.mode === mode;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    const advanced = document.getElementById("advanced-settings");
    if (advanced) {
      advanced.hidden = mode !== "detailed";
      if (mode === "detailed") advanced.open = true;
    }
  }

  function renderInspection(info) {
    state.info = info;
    const preview = document.getElementById("video-preview");
    const thumbnail = document.getElementById("video-thumbnail");
    const placeholder = document.getElementById("thumbnail-placeholder");
    document.getElementById("video-title").textContent = info.title || "YouTube video";
    document.getElementById("video-uploader").textContent = info.uploader ? `投稿者：${info.uploader}` : "";
    document.getElementById("video-duration").textContent = `再生時間：${info.duration_label || "時間不明"}`;
    if (info.thumbnail) {
      thumbnail.src = info.thumbnail;
      thumbnail.alt = `${info.title || "動画"}のサムネイル`;
      thumbnail.hidden = false;
      placeholder.hidden = true;
    } else {
      thumbnail.removeAttribute("src");
      thumbnail.hidden = true;
      placeholder.hidden = false;
    }

    const heightItems = (info.video_options || []).map((item) => ({
      value: item.height,
      label: `${item.label}（目安 ${item.estimated_size_label || "容量不明"}）`,
    }));
    populateSelect(document.getElementById("video-height"), heightItems, selectedSimpleHeight());
    const audioLabels = info.audio_format_labels || {};
    const audioItems = (info.audio_options || ["mp3", "m4a", "opus"].map((format) => ({ format })))
      .filter((item) => info.audio_formats?.[item.format] !== false && info.audio_formats?.[item.format] !== undefined)
      .map((item) => {
        const label = item.label || audioLabels[item.format] || item.format.toUpperCase();
        const size = item.estimated_size_label ? `・${item.estimated_size_label}` : "";
        return { value: item.format, label: `${label}${size}` };
      });
    if (!audioItems.length) audioItems.push({ value: "mp3", label: "音声形式を取得できません", disabled: true });
    populateSelect(document.getElementById("audio-format"), audioItems, "mp3");
    if (preview) preview.hidden = false;
    updateTargetUI();
    preview?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function renderJob(job) {
    const panel = document.getElementById("job-panel");
    const result = document.getElementById("job-result");
    const progress = document.getElementById("job-progress");
    const progressLabel = document.getElementById("job-progress-label");
    const message = document.getElementById("job-message");
    const heading = document.getElementById("job-heading");
    const cancel = document.getElementById("cancel-job");
    if (!panel) return;
    panel.hidden = false;
    const percentage = Number(job.progress || 0);
    progress.value = percentage;
    progressLabel.textContent = `${percentage}%`;
    message.textContent = job.message || job.stage_label || "処理中です";
    cancel.hidden = !job.can_cancel;
    document.querySelectorAll(".progress-steps li").forEach((item) => {
      item.classList.remove("is-current", "is-done");
      const order = ["waiting", "downloading", "processing", "completed"];
      const currentStage = job.status === "completed" ? "completed" : (job.stage || job.status);
      const itemIndex = order.indexOf(item.dataset.stage);
      const currentIndex = order.indexOf(currentStage);
      if (currentIndex >= 0 && itemIndex < currentIndex) item.classList.add("is-done");
      if (currentIndex >= 0 && itemIndex === currentIndex) item.classList.add(job.status === "completed" ? "is-done" : "is-current");
    });
    if (job.status === "completed") {
      heading.textContent = "保存の準備ができました";
      result.hidden = false;
      document.getElementById("result-expiry").textContent = `${job.expires_at_label || "24時間以内"}まで保存できます（${job.file_size_label || "容量不明"}）`;
      const link = document.getElementById("download-link");
      link.href = job.download_url;
      link.setAttribute("download", job.filename || "youtube-file");
      const pickerButton = document.getElementById("save-picker-button");
      if (pickerButton) {
        pickerButton.dataset.savePickerUrl = job.download_url || "";
        pickerButton.dataset.savePickerFilename = job.filename || "youtube-file";
      }
    } else if (["failed", "cancelled", "interrupted", "expired"].includes(job.status)) {
      heading.textContent = job.status === "failed" ? "保存できませんでした" : (job.status_label || "処理が終了しました");
      result.hidden = true;
      if (job.message) setAlert(job.message, job.status === "failed" ? "error" : "info");
    }
  }

  function stopPolling() {
    if (state.pollTimer) window.clearTimeout(state.pollTimer);
    state.pollTimer = null;
  }

  async function pollJob() {
    if (!state.jobId) return;
    try {
      const job = await requestJson(`/api/jobs/${state.jobId}`);
      renderJob(job);
      if (["completed", "failed", "cancelled", "interrupted", "expired"].includes(job.status)) {
        stopPolling();
        return;
      }
      state.pollTimer = window.setTimeout(pollJob, 2000);
    } catch (error) {
      stopPolling();
      setAlert(error.message || "処理状況を確認できませんでした");
    }
  }

  function initHistoryActions() {
    document.querySelectorAll("[data-cancel-job]").forEach((button) => {
      button.addEventListener("click", async () => {
        if (!window.confirm("この処理を中止しますか？")) return;
        button.disabled = true;
        try {
          await requestJson(`/api/jobs/${button.dataset.cancelJob}/cancel`, { method: "POST" });
          window.location.reload();
        } catch (error) {
          button.disabled = false;
          window.alert(error.message || "中止できませんでした");
        }
      });
    });
  }

  function initDownloader() {
    const inspectForm = document.getElementById("inspect-form");
    if (!inspectForm) return;
    const inspectButton = document.getElementById("inspect-button");
    const submitButton = document.getElementById("submit-job");
    const resetButton = document.getElementById("reset-workflow");
    const urlInput = document.getElementById("youtube-url");
    state.mode = window.localStorage.getItem("yt-mode") === "detailed" ? "detailed" : "simple";
    updateMode(state.mode);

    document.querySelectorAll("[data-mode]").forEach((button) => {
      button.addEventListener("click", () => updateMode(button.dataset.mode));
    });
    document.querySelectorAll('input[name="target"]').forEach((input) => input.addEventListener("change", updateTargetUI));
    document.getElementById("audio-format")?.addEventListener("change", updateTargetUI);

    inspectForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const url = urlInput.value.trim();
      if (!url) {
        setAlert("YouTubeのリンクを入力してください");
        urlInput.focus();
        return;
      }
      setAlert("");
      setButtonBusy(inspectButton, true, "確認しています…");
      try {
        const info = await requestJson("/api/inspect", {
          method: "POST",
          body: JSON.stringify({ url }),
        });
        renderInspection(info);
      } catch (error) {
        setAlert(error.message || "動画を確認できませんでした");
      } finally {
        setButtonBusy(inspectButton, false);
      }
    });

    submitButton?.addEventListener("click", async () => {
      if (!state.info) {
        setAlert("先に動画を確認してください");
        return;
      }
      const target = selectedTarget();
      const detailed = state.mode === "detailed";
      const videoHeight = Number(document.getElementById("video-height")?.value || selectedSimpleHeight());
      const body = {
        video_id: state.info.video_id,
        mode: state.mode,
        target,
        video_format: detailed ? (document.getElementById("video-format")?.value || "mp4") : "mp4",
        audio_format: detailed ? (document.getElementById("audio-format")?.value || "mp3") : "mp3",
        max_height: detailed && target === "video" ? videoHeight : selectedSimpleHeight(),
        mp3_quality: detailed && target === "audio"
          ? Number(document.getElementById("mp3-quality")?.value || 192)
          : (target === "audio" ? 320 : 192),
      };
      setAlert("");
      setButtonBusy(submitButton, true, "準備しています…");
      try {
        const job = await requestJson("/api/jobs", { method: "POST", body: JSON.stringify(body) });
        state.jobId = job.id;
        document.getElementById("job-result").hidden = true;
        renderJob(job);
        stopPolling();
        state.pollTimer = window.setTimeout(pollJob, 500);
        document.getElementById("job-panel")?.scrollIntoView({ behavior: "smooth", block: "start" });
      } catch (error) {
        setAlert(error.message || "保存の準備を始められませんでした");
      } finally {
        setButtonBusy(submitButton, false);
      }
    });

    document.getElementById("cancel-job")?.addEventListener("click", async () => {
      if (!state.jobId) return;
      const button = document.getElementById("cancel-job");
      button.disabled = true;
      try {
        const job = await requestJson(`/api/jobs/${state.jobId}/cancel`, { method: "POST" });
        renderJob(job);
        if (job.status === "cancelled") stopPolling();
      } catch (error) {
        setAlert(error.message || "処理を中止できませんでした");
        button.disabled = false;
      }
    });

    resetButton?.addEventListener("click", () => {
      stopPolling();
      state.info = null;
      state.jobId = null;
      inspectForm.reset();
      document.getElementById("video-preview").hidden = true;
      document.getElementById("job-panel").hidden = true;
      document.getElementById("job-result").hidden = true;
      setAlert("");
      urlInput.focus();
    });
  }

  initLargeText();
  initDownloader();
  initHistoryActions();
  initSavePickerButtons();
})();
