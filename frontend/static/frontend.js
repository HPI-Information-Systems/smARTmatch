(() => {
  function clamp(value, min, max) {
    if (max < min) return (min + max) / 2;
    return Math.max(min, Math.min(value, max));
  }

  function getDisplayedImageRect(img) {
    const rect = img.getBoundingClientRect();
    const style = window.getComputedStyle(img);
    const naturalWidth = img.naturalWidth || rect.width;
    const naturalHeight = img.naturalHeight || rect.height;

    if (
      style.objectFit !== "contain" ||
      naturalWidth <= 0 ||
      naturalHeight <= 0 ||
      rect.width <= 0 ||
      rect.height <= 0
    ) {
      return rect;
    }

    const naturalRatio = naturalWidth / naturalHeight;
    const boxRatio = rect.width / rect.height;
    let width = rect.width;
    let height = rect.height;
    let left = rect.left;
    let top = rect.top;

    if (naturalRatio > boxRatio) {
      height = rect.width / naturalRatio;
      top += (rect.height - height) / 2;
    } else {
      width = rect.height * naturalRatio;
      left += (rect.width - width) / 2;
    }

    return {
      left,
      top,
      right: left + width,
      bottom: top + height,
      width,
      height,
    };
  }

  function setupZoom(wrap) {
    if (!wrap || wrap.dataset.zoomBound === "1") return;

    const img = wrap.querySelector("img");
    const lens = wrap.querySelector(".zoom-lens");
    if (!img || !lens) return;

    wrap.dataset.zoomBound = "1";
    const zoom = Number(wrap.dataset.zoom || 2.5);

    function hideLens() {
      lens.style.display = "none";
    }

    function moveLens(event) {
      const rect = getDisplayedImageRect(img);
      const wrapRect = wrap.getBoundingClientRect();
      const x = event.clientX - rect.left;
      const y = event.clientY - rect.top;

      if (x < 0 || y < 0 || x > rect.width || y > rect.height) {
        hideLens();
        return;
      }

      const lensWidth = lens.offsetWidth || 220;
      const lensHeight = lens.offsetHeight || lensWidth;
      const halfWidth = lensWidth / 2;
      const halfHeight = lensHeight / 2;
      const left = event.clientX - wrapRect.left - halfWidth;
      const top = event.clientY - wrapRect.top - halfHeight;
      const zoomSrc = img.dataset.zoomSrc || img.currentSrc || img.src;

      lens.style.display = "block";
      lens.style.left = `${left}px`;
      lens.style.top = `${top}px`;
      lens.style.backgroundImage = `url('${zoomSrc}')`;
      lens.style.backgroundSize = `${rect.width * zoom}px ${rect.height * zoom}px`;
      lens.style.backgroundPosition = `-${x * zoom - halfWidth}px -${y * zoom - halfHeight}px`;
    }

    wrap.addEventListener("pointermove", moveLens);
    wrap.addEventListener("pointerleave", hideLens);
    wrap.addEventListener("pointercancel", hideLens);
    wrap.addEventListener("mouseleave", hideLens);
    img.addEventListener("load", hideLens);
    document.addEventListener("scroll", hideLens, true);
    window.addEventListener("blur", hideLens);
  }

  function initZoomWrappers(root = document) {
    if (root.matches && root.matches(".zoom-wrap")) {
      setupZoom(root);
    }
    root.querySelectorAll(".zoom-wrap").forEach(setupZoom);
  }

  function setupSimilaritySlider(slider) {
    if (!slider || slider.dataset.weightBound === "1") return;

    slider.dataset.weightBound = "1";
    const control = slider.closest(".similarity-weight-control") || document;

    function update() {
      const imageWeight = clamp(Number(slider.value) || 0, 0, 100);
      slider.style.setProperty("--image-weight", `${imageWeight}%`);
      control.querySelectorAll("[data-image-weight-output]").forEach((item) => {
        item.textContent = String(Math.round(imageWeight));
      });
      control.querySelectorAll("[data-metadata-weight-output]").forEach((item) => {
        item.textContent = String(Math.round(100 - imageWeight));
      });
    }

    slider.addEventListener("input", update);
    update();
  }

  function initSimilaritySliders(root = document) {
    if (root.matches && root.matches(".similarity-weight-slider")) {
      setupSimilaritySlider(root);
    }
    root.querySelectorAll(".similarity-weight-slider").forEach(setupSimilaritySlider);
  }

  function isInteractiveTarget(target) {
    return Boolean(
      target.closest(
        "a, button, input, select, textarea, label, form, [role='button'], [data-no-card-click]"
      )
    );
  }

  function setupClickableMatchItem(item) {
    if (!item || item.dataset.clickBound === "1") return;
    const href = item.dataset.href;
    if (!href) return;

    item.dataset.clickBound = "1";
    item.addEventListener("click", (event) => {
      if (isInteractiveTarget(event.target)) return;
      window.location.href = href;
    });
    item.addEventListener("keydown", (event) => {
      if ((event.key !== "Enter" && event.key !== " ") || isInteractiveTarget(event.target)) {
        return;
      }
      event.preventDefault();
      window.location.href = href;
    });
  }

  function initClickableMatchItems(root = document) {
    if (root.matches && root.matches(".match-list-item[data-href]")) {
      setupClickableMatchItem(root);
    }
    root.querySelectorAll(".match-list-item[data-href]").forEach(setupClickableMatchItem);
  }

  function setupDetailsMoreToggle(button) {
    if (!button || button.dataset.moreBound === "1") return;
    const section = button.closest(".match-details-section") || document;
    const rows = Array.from(section.querySelectorAll(".match-extra-detail-row"));
    if (!rows.length) return;

    button.dataset.moreBound = "1";
    const moreLabel = button.dataset.moreLabel || "Mehr anzeigen";
    const lessLabel = button.dataset.lessLabel || "Weniger anzeigen";

    function setExpanded(expanded) {
      rows.forEach((row) => row.classList.toggle("d-none", !expanded));
      button.setAttribute("aria-expanded", String(expanded));
      button.textContent = expanded ? lessLabel : moreLabel;
    }

    button.addEventListener("click", () => {
      setExpanded(button.getAttribute("aria-expanded") !== "true");
    });
    setExpanded(button.getAttribute("aria-expanded") === "true");
  }

  function initDetailsMoreToggles(root = document) {
    if (root.matches && root.matches(".match-details-more-toggle")) {
      setupDetailsMoreToggle(root);
    }
    root.querySelectorAll(".match-details-more-toggle").forEach(setupDetailsMoreToggle);
  }

  function setupCenteredScroll(container) {
    if (!container || container.dataset.centerScrollBound === "1") return;

    container.dataset.centerScrollBound = "1";
    const center = () => {
      if (container.scrollWidth <= container.clientWidth) return;
      container.scrollLeft = (container.scrollWidth - container.clientWidth) / 2;
    };

    requestAnimationFrame(center);
    window.addEventListener("resize", center);
  }

  function initCenteredScroll(root = document) {
    if (root.matches && root.matches("[data-center-scroll]")) {
      setupCenteredScroll(root);
    }
    root.querySelectorAll("[data-center-scroll]").forEach(setupCenteredScroll);
  }

  const STATS_TIME_FORMATTERS = {
    date: new Intl.DateTimeFormat("de-DE", {
      day: "2-digit",
      month: "2-digit",
      year: "2-digit",
    }),
    time: new Intl.DateTimeFormat("de-DE", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }),
  };

  function localStatsTimeLabel(element) {
    const isoValue = element.dataset.statsTime;
    if (!isoValue) return null;
    const date = new Date(isoValue);
    if (Number.isNaN(date.getTime())) return null;
    const formatter = STATS_TIME_FORMATTERS[element.dataset.statsTimeFormat] || STATS_TIME_FORMATTERS.time;
    return formatter.format(date);
  }

  function updateStatsTimeLabel(element) {
    const label = localStatsTimeLabel(element);
    if (!label) return;
    element.textContent = `${label}${element.dataset.statsTitleSuffix || ""}`;
  }

  function initStatsTimeLabels(root = document) {
    if (root.matches && root.matches("[data-stats-time]")) {
      updateStatsTimeLabel(root);
    }
    root.querySelectorAll("[data-stats-time]").forEach(updateStatsTimeLabel);
  }

  const SYSTEM_STATUS_POLL_INTERVAL_MS = 1000;
  const SYSTEM_STATUS_MAX_ATTEMPTS = 120;

  function dismissSystemToast(toast) {
    if (!toast || toast.dataset.dismissing === "1") return;
    toast.dataset.dismissing = "1";
    toast.classList.add("system-toast-exit");
    window.setTimeout(() => toast.remove(), 220);
  }

  function showMissingFilesToast(missingLabel) {
    const container = document.querySelector(".system-toast-container");
    if (!container || container.querySelector("[data-missing-files-toast]")) return;

    const toast = document.createElement("div");
    toast.className = "system-toast system-toast-error";
    toast.dataset.missingFilesToast = "1";
    toast.setAttribute("role", "alert");
    toast.innerHTML = `
      <div class="system-toast-icon" aria-hidden="true"><i class="bi bi-exclamation-circle-fill"></i></div>
      <div class="system-toast-content">
        <strong class="system-toast-title">Bilddateien nicht verfügbar</strong>
        <p class="system-toast-message">${missingLabel} in der Datenbank hinterlegte Bildpfade konnten auf diesem System nicht gefunden werden.</p>
      </div>
      <button class="system-toast-close" type="button" aria-label="Benachrichtigung schließen">×</button>
      <div class="system-toast-progress" aria-hidden="true"></div>
    `;
    toast.querySelector(".system-toast-close").addEventListener("click", () => dismissSystemToast(toast));
    container.appendChild(toast);
    window.setTimeout(() => dismissSystemToast(toast), 10000);
  }

  async function loadSystemStatus(attempt = 0) {
    const statusUrl = document.body.dataset.systemStatusUrl;
    if (!statusUrl) return;

    try {
      const response = await fetch(statusUrl, {
        headers: { Accept: "application/json" },
        cache: "no-store",
      });
      if (!response.ok) return;
      const payload = await response.json();
      const imageFiles = payload && payload.image_files;
      const projectSize = payload && payload.project_size;
      if (projectSize && projectSize.scan_ready) {
        document.querySelectorAll("[data-project-size-label]").forEach((label) => {
          label.textContent = projectSize.size_label;
          label.classList.remove("stats-kpi-value-loading");
        });
      }
      if (imageFiles && imageFiles.scan_ready && Number(imageFiles.missing_count) > 0) {
        showMissingFilesToast(imageFiles.missing_label);
      }
      if (
        (!imageFiles || !imageFiles.scan_ready || !projectSize || !projectSize.scan_ready) &&
        attempt < SYSTEM_STATUS_MAX_ATTEMPTS
      ) {
        window.setTimeout(() => loadSystemStatus(attempt + 1), SYSTEM_STATUS_POLL_INTERVAL_MS);
      }
    } catch (_error) {
      // Do not interrupt the page if the informational status check fails.
    }
  }

  const KEYPOINT_COLORS = {
    lostPoint: "#00d1ff",
    auctionPoint: "#ffb000",
    pointStroke: "rgba(15, 23, 42, 0.85)",
    matchLine: "50, 205, 50",
    imageBorder: "rgba(15, 23, 42, 0.18)",
  };

  function parseKeypointPayload(viewer) {
    try {
      return JSON.parse(viewer.dataset.keypointMatches || "{}");
    } catch (_error) {
      return {};
    }
  }

  function normalizedPoint(value) {
    if (!value || typeof value !== "object") return null;
    const x = Number(value.x);
    const y = Number(value.y);
    if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
    return { x, y };
  }

  function normalizedKeypointMatches(payload) {
    const rows = payload && Array.isArray(payload.matches) ? payload.matches : [];
    return rows
      .map((row) => {
        const lost = normalizedPoint(row && row.lost);
        const auction = normalizedPoint(row && row.auction);
        if (!lost || !auction) return null;
        const score = Number(row && row.score);
        return {
          lost,
          auction,
          score: Number.isFinite(score) ? clamp(score, 0, 1) : null,
        };
      })
      .filter(Boolean);
  }

  function loadCanvasImage(src) {
    return new Promise((resolve, reject) => {
      if (!src) {
        reject(new Error("Missing image source"));
        return;
      }
      const image = new Image();
      image.decoding = "async";
      image.onload = () => resolve(image);
      image.onerror = () => reject(new Error(`Could not load image: ${src}`));
      image.src = src;
    });
  }

  function setKeypointStatus(viewer, text, visible = true) {
    const status = viewer.querySelector("[data-keypoint-status]");
    if (!status) return;
    status.textContent = text;
    status.style.display = visible ? "block" : "none";
  }

  function imageSize(image) {
    return {
      width: image.naturalWidth || image.width || 1,
      height: image.naturalHeight || image.height || 1,
    };
  }

  function keypointImageBoxes(width, height, lostImage, auctionImage) {
    const lostSize = imageSize(lostImage);
    const auctionSize = imageSize(auctionImage);
    const gap = clamp(width * 0.04, 24, 56);
    const padding = clamp(Math.min(width, height) * 0.025, 10, 28);
    const availableWidth = Math.max(1, width - gap - padding * 2);
    const availableHeight = Math.max(1, height - padding * 2);
    const scale = Math.min(
      availableWidth / (lostSize.width + auctionSize.width),
      availableHeight / Math.max(lostSize.height, auctionSize.height)
    );
    const safeScale = Number.isFinite(scale) && scale > 0 ? scale : 1;
    const lostWidth = lostSize.width * safeScale;
    const lostHeight = lostSize.height * safeScale;
    const auctionWidth = auctionSize.width * safeScale;
    const auctionHeight = auctionSize.height * safeScale;
    const totalWidth = lostWidth + gap + auctionWidth;
    const left = (width - totalWidth) / 2;

    return {
      lost: {
        x: left,
        y: (height - lostHeight) / 2,
        width: lostWidth,
        height: lostHeight,
      },
      auction: {
        x: left + lostWidth + gap,
        y: (height - auctionHeight) / 2,
        width: auctionWidth,
        height: auctionHeight,
      },
    };
  }

  function canvasPoint(point, image, box) {
    const size = imageSize(image);
    return {
      x: box.x + (point.x / size.width) * box.width,
      y: box.y + (point.y / size.height) * box.height,
    };
  }

  function drawImageFrame(ctx, image, box) {
    ctx.drawImage(image, box.x, box.y, box.width, box.height);
    ctx.strokeStyle = KEYPOINT_COLORS.imageBorder;
    ctx.lineWidth = 1;
    ctx.strokeRect(box.x, box.y, box.width, box.height);
  }

  function drawKeypoint(ctx, point, color) {
    ctx.beginPath();
    ctx.arc(point.x, point.y, 3.2, 0, Math.PI * 2);
    ctx.fillStyle = color;
    ctx.fill();
    ctx.lineWidth = 1.4;
    ctx.strokeStyle = KEYPOINT_COLORS.pointStroke;
    ctx.stroke();
  }

  function drawKeypointLine(ctx, lostPoint, auctionPoint, score) {
    const alpha = score === null ? 0.62 : 0.25 + score * 0.55;
    const width = score === null ? 0.9 : 0.55 + score * 1.15;
    ctx.beginPath();
    ctx.moveTo(lostPoint.x, lostPoint.y);
    ctx.lineTo(auctionPoint.x, auctionPoint.y);
    ctx.strokeStyle = `rgba(${KEYPOINT_COLORS.matchLine}, ${alpha})`;
    ctx.lineWidth = width;
    ctx.stroke();
  }

  function drawKeypointViewer(viewer) {
    const state = viewer._smartmatchKeypoints;
    if (!state || !state.images) return;

    const stage = viewer.querySelector(".keypoint-canvas-stage");
    const canvas = viewer.querySelector(".keypoint-match-canvas");
    if (!stage || !canvas) return;

    const rect = stage.getBoundingClientRect();
    const width = Math.floor(rect.width || stage.clientWidth || 0);
    const height = Math.floor(rect.height || stage.clientHeight || 0);
    if (width < 4 || height < 4) return;

    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;

    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = "#f8fafc";
    ctx.fillRect(0, 0, width, height);

    const boxes = keypointImageBoxes(width, height, state.images.lost, state.images.auction);
    drawImageFrame(ctx, state.images.lost, boxes.lost);
    drawImageFrame(ctx, state.images.auction, boxes.auction);

    const mappedMatches = state.matches.map((match) => ({
      lost: canvasPoint(match.lost, state.images.lost, boxes.lost),
      auction: canvasPoint(match.auction, state.images.auction, boxes.auction),
      score: match.score,
    }));

    mappedMatches.forEach((match) => {
      drawKeypoint(ctx, match.lost, KEYPOINT_COLORS.lostPoint);
      drawKeypoint(ctx, match.auction, KEYPOINT_COLORS.auctionPoint);
    });
    mappedMatches.forEach((match) => {
      drawKeypointLine(ctx, match.lost, match.auction, match.score);
    });

    setKeypointStatus(
      viewer,
      mappedMatches.length ? "" : "Keine Keypoint-Matches vorhanden.",
      mappedMatches.length === 0
    );
  }

  function scheduleKeypointDraw(viewer) {
    const state = viewer._smartmatchKeypoints;
    if (!state || state.drawFrame) return;
    state.drawFrame = window.requestAnimationFrame(() => {
      state.drawFrame = 0;
      drawKeypointViewer(viewer);
    });
  }

  function setupKeypointViewer(viewer) {
    if (!viewer || viewer.dataset.keypointBound === "1") return;
    viewer.dataset.keypointBound = "1";
    const payload = parseKeypointPayload(viewer);
    const state = {
      matches: normalizedKeypointMatches(payload),
      images: null,
      drawFrame: 0,
      resizeObserver: null,
    };
    viewer._smartmatchKeypoints = state;

    Promise.all([
      loadCanvasImage(viewer.dataset.lostImageSrc),
      loadCanvasImage(viewer.dataset.auctionImageSrc),
    ])
      .then(([lost, auction]) => {
        state.images = { lost, auction };
        setKeypointStatus(viewer, "", false);
        scheduleKeypointDraw(viewer);
      })
      .catch(() => {
        setKeypointStatus(viewer, "Die Keypoint-Bilder konnten nicht geladen werden.");
      });

    const modal = viewer.closest(".modal");
    if (modal) {
      modal.addEventListener("shown.bs.modal", () => scheduleKeypointDraw(viewer));
    }
    window.addEventListener("resize", () => scheduleKeypointDraw(viewer));

    const stage = viewer.querySelector(".keypoint-canvas-stage");
    if (stage && "ResizeObserver" in window) {
      state.resizeObserver = new ResizeObserver(() => scheduleKeypointDraw(viewer));
      state.resizeObserver.observe(stage);
    }
  }

  function initKeypointViewers(root = document) {
    if (root.matches && root.matches("[data-keypoint-viewer]")) {
      setupKeypointViewer(root);
    }
    root.querySelectorAll("[data-keypoint-viewer]").forEach(setupKeypointViewer);
  }

  function normalizedSearchValue(value) {
    return String(value || "").trim().replace(/\s+/g, " ");
  }

  function setSearchResultsPageMode() {
    document.body.classList.remove("is-home-page", "is-match-page");
    document.body.classList.add("is-sub-page");
  }

  function showSearchResultsLoading(form) {
    const input = form.querySelector('input[name="search"]');
    const target = document.querySelector("#frontend-main-content");
    const template = document.querySelector("#search-results-loading-template");
    if (!input || !target || !template) return;

    const search = normalizedSearchValue(input.value);
    let state = target.querySelector("[data-search-results-loading]");
    if (!state) {
      target.replaceChildren(template.content.cloneNode(true));
      state = target.querySelector("[data-search-results-loading]");
    }
    if (!state) return;

    const heading = state.querySelector("[data-search-loading-heading]");
    const message = state.querySelector("[data-search-loading-message]");
    const spinner = state.querySelector("[data-search-loading-spinner]");
    if (heading) {
      heading.textContent = search ? `Suchergebnisse für „${search}“` : "Matches";
    }
    if (message) {
      message.textContent = search
        ? "Suchergebnisse werden geladen …"
        : "Matches werden geladen …";
      message.closest("[role]")?.setAttribute("role", "status");
    }
    spinner?.classList.remove("d-none");
    target.setAttribute("aria-busy", "true");
    setSearchResultsPageMode();
  }

  function showSearchResultsError() {
    const target = document.querySelector("#frontend-main-content");
    const state = target && target.querySelector("[data-search-results-loading]");
    if (!state) return;

    state.querySelector("[data-search-loading-spinner]")?.classList.add("d-none");
    const message = state.querySelector("[data-search-loading-message]");
    if (message) {
      message.textContent = "Die Suchergebnisse konnten nicht geladen werden. Bitte erneut versuchen.";
      message.closest("[role]")?.setAttribute("role", "alert");
    }
    target.setAttribute("aria-busy", "false");
  }

  function setupSearchResultsLoading(form) {
    if (!form || form.dataset.searchLoadingBound === "1") return;
    const input = form.querySelector('input[name="search"]');
    if (!input) return;

    form.dataset.searchLoadingBound = "1";
    input.addEventListener("input", (event) => {
      if (!event.isComposing) showSearchResultsLoading(form);
    });
    input.addEventListener("compositionend", () => showSearchResultsLoading(form));
    form.addEventListener("submit", () => showSearchResultsLoading(form));
  }

  function initSearchResultsLoading(root = document) {
    if (root.matches && root.matches("[data-search-results-form]")) {
      setupSearchResultsLoading(root);
    }
    root.querySelectorAll("[data-search-results-form]").forEach(setupSearchResultsLoading);
  }

  function initFrontend(root = document) {
    initZoomWrappers(root);
    initSimilaritySliders(root);
    initClickableMatchItems(root);
    initDetailsMoreToggles(root);
    initCenteredScroll(root);
    initStatsTimeLabels(root);
    initKeypointViewers(root);
    initSearchResultsLoading(root);
  }

  document.addEventListener("DOMContentLoaded", () => {
    initFrontend();
    loadSystemStatus();
  });

  document.body.addEventListener("htmx:afterSwap", (event) => {
    initFrontend(event.target || document);
  });

  document.body.addEventListener("htmx:afterRequest", (event) => {
    const form = document.querySelector("[data-search-results-form]");
    const requestElement = event.detail && event.detail.elt;
    if (
      form &&
      event.detail &&
      event.detail.successful === false &&
      (requestElement === form || (requestElement && form.contains(requestElement)))
    ) {
      showSearchResultsError();
    }
  });
})();
