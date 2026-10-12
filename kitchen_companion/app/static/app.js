const state = {
  recipes: [],
  selected: null,
  servings: 2,
  step: 0,
  timer: null,
  timerEnds: 0,
  timerPending: false,
  recognition: null,
  cookRecognition: null,
  cookVoiceEnabled: false,
  cookVoicePaused: false,
  cookVoiceRestartTimer: null,
  cookVoiceWatchdogTimer: null,
  cookVoiceSpeechResumeTimer: null,
  cookVoiceRetryCount: 0,
  cookVoiceStarting: false,
  cookVoiceLastNetworkNotice: 0,
  editorImage: "",
  runtime: null,
  activeMode: "assist",
  sourceFilter: "all",
  catalogStatus: null,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

async function api(path, options = {}) {
  const endpoint = new URL(path.replace(/^\//, ""), document.baseURI);
  const response = await fetch(endpoint, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.error || "通信に失敗しました");
    error.httpStatus = response.status;
    error.data = data;
    throw error;
  }
  return data;
}

function escapeHtml(value = "") {
  return String(value).replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  }[char]));
}

function safeUrl(value = "") {
  try {
    const url = new URL(value.replace(/^\//, ""), document.baseURI);
    return ["http:", "https:"].includes(url.protocol) ? url.href : "";
  } catch { return ""; }
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(showToast.timeout);
  showToast.timeout = setTimeout(() => toast.classList.remove("show"), 2800);
}

function newRequestId() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map((value) => value.toString(16).padStart(2, "0"));
  return `${hex.slice(0, 4).join("")}-${hex.slice(4, 6).join("")}-${hex.slice(6, 8).join("")}-${hex.slice(8, 10).join("")}-${hex.slice(10).join("")}`;
}

function formatNumber(value) {
  if (value == null || Number.isNaN(Number(value))) return "";
  const number = Number(value);
  const common = [[0.25, "1/4"], [0.333, "1/3"], [0.5, "1/2"], [0.667, "2/3"], [0.75, "3/4"]];
  const whole = Math.floor(number);
  const fraction = number - whole;
  const match = common.find(([candidate]) => Math.abs(fraction - candidate) < 0.025);
  if (match) return `${whole || ""}${whole ? " " : ""}${match[1]}`;
  return new Intl.NumberFormat("ja-JP", { maximumFractionDigits: 2 }).format(number);
}

function adjustServings(current, direction) {
  const value = Number(current);
  return Math.max(0.5, Math.round((value + direction * 0.5) * 2) / 2);
}

function formatServings(value) {
  const number = Number(value);
  if (Number.isInteger(number)) return formatNumber(number);
  if (Number.isInteger(number * 2)) return `${Math.round(number * 2)}/2`;
  return formatNumber(number);
}

function scaledIngredient(item, base, target, canScale = true) {
  if (item.amount == null) return item.note || "適量";
  if (!canScale) return `${formatNumber(item.amount)}${item.unit ? ` ${item.unit}` : ""}`;
  return `${formatNumber(Number(item.amount) * target / base)}${item.unit ? ` ${item.unit}` : ""}`;
}

async function loadRecipes(query = "") {
  const result = await api(`/api/catalog/recipes?q=${encodeURIComponent(query)}`);
  state.recipes = result.recipes;
  state.catalogStatus = result;
  renderGrid();
  renderCatalogStatus();
}

async function loadRuntime() {
  state.runtime = await api("/api/runtime");
  $("#runtimeStatus").innerHTML = `<span></span> ${escapeHtml(state.runtime.label)}`;
  renderCatalogStatus();
}

function renderCatalogStatus() {
  const element = $("#catalogStatus");
  if (!element) return;
  element.className = "catalog-status";
  if (!state.runtime || !state.catalogStatus) {
    element.textContent = "Mealieの接続状態を確認中です";
    return;
  }
  if (state.catalogStatus.mealie_status === "error") {
    element.classList.add("error");
    element.innerHTML = `<span>${escapeHtml(state.catalogStatus.mealie_message)}</span><button data-action="refresh-recipes">再読み込み</button>`;
    return;
  }
  if (state.catalogStatus.mealie_status === "mock") {
    element.classList.add("warning");
    element.innerHTML = '<span>現在はMealieモックです。実レシピを表示するにはApp設定を <strong>home_assistant</strong> に変更して再起動してください。</span><button data-action="refresh-recipes">再読み込み</button>';
    return;
  }
  const count = Number(state.catalogStatus.mealie_count || 0);
  element.innerHTML = `<span>Mealie接続中：${count}件を取得しました</span><button data-action="refresh-recipes">更新</button>`;
}

function renderGrid() {
  const grid = $("#recipeGrid");
  const recipes = state.recipes.filter((recipe) => {
    if (state.sourceFilter === "mealie") return ["mealie", "mealie_mock"].includes(recipe.source_kind);
    if (state.sourceFilter === "url") return Boolean(recipe.source_url || recipe.video_url);
    return true;
  });
  if (!recipes.length) {
    grid.innerHTML = '<div class="empty-state"><strong>該当するレシピはありません</strong><p>別の材料で探すか、新しいレシピを登録してください。</p></div>';
    return;
  }
  grid.innerHTML = recipes.map((recipe) => {
    const image = safeUrl(recipe.image_url);
    const media = image ? `<img src="${escapeHtml(image)}" alt="" loading="lazy">` : '<div class="placeholder">♨</div>';
    const servingLabel = recipe.servings_confirmed === false ? "人数未確認" : `${formatServings(recipe.servings)}人分`;
    const sourceLabel = ["mealie", "mealie_mock"].includes(recipe.source_kind)
      ? (recipe.source_kind === "mealie_mock" ? "Mealieモック" : "Mealie")
      : recipe.video_url ? "動画URL" : recipe.source_url ? "Web URL" : "手入力";
    return `<article class="recipe-card">
      <div class="recipe-card-image">${media}<span>${servingLabel} · ${recipe.steps.length}工程</span></div>
      <div class="recipe-card-body"><h3>${escapeHtml(recipe.title)}</h3><p>${escapeHtml(recipe.description || "自分のレシピノート")}</p>
      <div class="tags"><span class="tag">${sourceLabel}</span>${recipe.tags.slice(0, 2).map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("")}</div>
      <button class="card-button" data-open="${recipe.id}">レシピを見る</button></div></article>`;
  }).join("");
}

function openDetail(recipe) {
  setMode("assist", false);
  state.selected = recipe;
  state.servings = recipe.servings;
  renderDetail();
  $("#homeView").classList.add("hidden");
  $("#detailView").classList.remove("hidden");
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function setSourceFilter(source) {
  state.sourceFilter = ["all", "mealie", "url"].includes(source) ? source : "all";
  $$('[data-source]').forEach((button) => {
    const active = button.dataset.source === state.sourceFilter;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  renderGrid();
}

function setMode(mode, focus = true) {
  state.activeMode = mode === "import" ? "import" : "assist";
  const importing = state.activeMode === "import";
  $("#assistView").classList.toggle("hidden", importing);
  $("#importView").classList.toggle("hidden", !importing);
  $$('[data-mode]').forEach((button) => {
    const active = button.dataset.mode === state.activeMode;
    button.classList.toggle("active", active);
    if (button.closest(".mode-switcher")) button.setAttribute("aria-pressed", String(active));
  });
  if (focus) window.scrollTo({ top: 0, behavior: "smooth" });
}

function renderDetail() {
  const recipe = state.selected;
  if (!recipe) return;
  const image = safeUrl(recipe.image_url);
  const source = safeUrl(recipe.source_url);
  const video = safeUrl(recipe.video_url);
  const servingsConfirmed = recipe.servings_confirmed !== false;
  const syncButton = !recipe.read_only && state.runtime?.mealie_sync_enabled
    ? recipe.mealie_sync_status === "synced"
      ? '<button class="secondary-button" disabled>Mealie同期済み</button>'
      : recipe.mealie_sync_status === "outcome_unknown"
        ? '<button class="secondary-button" disabled>Mealieで確認</button>'
        : '<button class="secondary-button" data-action="sync-mealie">Mealieへ同期</button>'
    : "";
  const deleteControl = recipe.read_only
    ? '<p class="delete-guidance">このレシピを削除する場合はMealie側で削除し、一覧の「更新」を押してください。</p>'
    : '<button class="danger-button" data-action="delete-current">ローカル下書きを削除</button>';
  $("#detailView").innerHTML = `
    <button class="back-button" data-action="home">← レシピ一覧</button>
    <div class="detail-hero">
      <div class="detail-image">${image ? `<img src="${escapeHtml(image)}" alt="${escapeHtml(recipe.title)}">` : ""}</div>
      <div class="detail-copy"><p class="eyebrow">READY TO COOK</p><h1>${escapeHtml(recipe.title)}</h1><p>${escapeHtml(recipe.description)}</p>
        <div class="serving-control">${servingsConfirmed ? `<button data-serving="-1" aria-label="人数を減らす">−</button><strong>${formatServings(state.servings)}人分</strong><button data-serving="1" aria-label="人数を増やす">＋</button>` : "<strong>人数・換算は未確認</strong>"}</div>
        <div class="detail-actions"><button class="primary-button" data-action="start-cooking">調理をはじめる</button><button class="secondary-button" data-action="edit-current">${recipe.read_only ? "下書きにコピー" : "編集する"}</button>${syncButton}${deleteControl}</div>
      </div>
    </div>
    <div class="recipe-columns"><section><p class="eyebrow">INGREDIENTS</p><h2>材料</h2><ul class="ingredient-list">
      ${recipe.ingredients.map((item) => `<li><span>${escapeHtml(item.name)}${item.note ? `<small> ${escapeHtml(item.note)}</small>` : ""}</span><strong>${escapeHtml(scaledIngredient(item, recipe.servings, state.servings, servingsConfirmed))}</strong></li>`).join("")}
      </ul>${source ? `<a class="source-link" href="${escapeHtml(source)}" target="_blank" rel="noopener noreferrer">出典ページを開く</a>` : ""}${video ? `<br><a class="source-link" href="${escapeHtml(video)}" target="_blank" rel="noopener noreferrer">動画を開く</a>` : ""}</section>
      <section><p class="eyebrow">DIRECTIONS</p><h2>作り方</h2><ol class="steps-list">${recipe.steps.map((step) => `<li>${escapeHtml(step)}</li>`).join("")}</ol></section></div>`;
}

async function deleteCurrentRecipe() {
  const recipe = state.selected;
  if (!recipe || recipe.read_only) return;
  const message = recipe.mealie_sync_status === "synced"
    ? "ローカル下書きだけを削除します。Mealieのレシピは削除されません。続けますか？"
    : `「${recipe.title}」のローカル下書きを削除しますか？`;
  if (!window.confirm(message)) return;
  try {
    await api(`/api/recipes/${recipe.id}`, { method: "DELETE" });
    state.selected = null;
    await loadRecipes();
    goHome();
    showToast("ローカル下書きを削除しました");
  } catch (error) {
    showToast(error.message);
  }
}

async function syncCurrentRecipe(button) {
  if (!state.selected || state.selected.read_only) return;
  button.disabled = true;
  button.textContent = "Mealieへ同期中…";
  try {
    await api("/api/mealie/sync", {
      method: "POST",
      body: JSON.stringify({ recipe_id: state.selected.id }),
    });
    showToast("Mealieへ同期しました");
    await loadRecipes();
    goHome();
  } catch (error) {
    state.selected.mealie_sync_status = error.data?.status || "";
    renderDetail();
    showToast(error.message);
  }
}

function goHome() {
  $("#detailView").classList.add("hidden");
  $("#homeView").classList.remove("hidden");
  setMode("assist");
}

function parseIngredientLine(line) {
  const parts = line.split("|").map((part) => part.trim());
  if (parts.length >= 3) {
    const amount = Number(parts[0]);
    return { amount: Number.isFinite(amount) ? amount : null, unit: parts[1], name: parts.slice(2).join(" | "), note: "" };
  }
  return { amount: null, unit: "", name: line.trim(), note: "" };
}

function openEditor(recipe = null, videoOnly = false) {
  state.editorImage = recipe?.image_url || "";
  $("#recipeId").value = recipe?.id || "";
  $("#editorTitle").textContent = videoOnly ? "動画レシピを登録" : recipe ? "レシピを編集" : "レシピを登録";
  $("#titleField").value = recipe?.title || "";
  $("#servingsField").value = recipe?.servings || 2;
  $("#tagsField").value = recipe?.tags?.join(", ") || "";
  $("#descriptionField").value = recipe?.description || "";
  $("#ingredientsField").value = recipe?.ingredients?.map((item) => item.amount == null ? `${item.name}${item.note ? `（${item.note}）` : ""}` : `${formatNumber(item.amount)} | ${item.unit} | ${item.name}`).join("\n") || "";
  $("#stepsField").value = recipe?.steps?.join("\n") || "";
  $("#sourceField").value = recipe?.source_url || "";
  $("#videoField").value = recipe?.video_url || "";
  $("#formMessage").textContent = "";
  $("#editorDialog").showModal();
  setTimeout(() => (videoOnly ? $("#videoField") : $("#titleField")).focus(), 50);
}

async function saveRecipe(event) {
  event.preventDefault();
  const id = $("#recipeId").value;
  const payload = {
    title: $("#titleField").value,
    servings: Number($("#servingsField").value),
    tags: $("#tagsField").value.split(",").map((tag) => tag.trim()).filter(Boolean),
    description: $("#descriptionField").value,
    ingredients: $("#ingredientsField").value.split("\n").map((line) => line.trim()).filter(Boolean).map(parseIngredientLine),
    steps: $("#stepsField").value.split("\n").map((line) => line.trim()).filter(Boolean),
    source_url: $("#sourceField").value,
    video_url: $("#videoField").value,
    image_url: state.editorImage,
  };
  try {
    const recipe = await api(id ? `/api/recipes/${id}` : "/api/recipes", { method: id ? "PUT" : "POST", body: JSON.stringify(payload) });
    $("#editorDialog").close();
    await loadRecipes();
    openDetail(recipe);
    showToast("レシピを保存しました");
  } catch (error) { $("#formMessage").textContent = error.message; }
}

async function importFromUrl(event) {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button");
  button.disabled = true;
  button.textContent = "読み込み中…";
  try {
    const draft = await api("/api/import", { method: "POST", body: JSON.stringify({ url: $("#importUrl").value }) });
    openEditor(draft);
    showToast("内容を確認して保存してください");
  } catch (error) { showToast(error.message); }
  finally { button.disabled = false; button.textContent = "内容を読み込む"; }
}

async function importFromVideo(event) {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button");
  button.disabled = true;
  button.textContent = "動画情報を取得中…";
  try {
    const draft = await api("/api/import/video", {
      method: "POST",
      body: JSON.stringify({ url: $("#videoImportUrl").value }),
    });
    openEditor(draft, true);
    showToast(draft.ingredients.length || draft.steps.length
      ? "動画ページのレシピ情報を自動入力しました"
      : "動画情報を入力しました。材料と工程を確認してください");
  } catch (error) { showToast(error.message); }
  finally { button.disabled = false; button.textContent = "動画から自動入力"; }
}

function recognitionErrorMessage(code) {
  const messages = {
    "not-allowed": "マイク権限が許可されていません。Androidの設定でHome Assistantのマイクを許可してください",
    "service-not-allowed": "この画面では音声認識サービスを利用できません。ChromeまたはGboardのマイク入力を試してください",
    "audio-capture": "マイクを使用できません。他のアプリが使用中でないか確認してください",
    "no-speech": "音声を検出できませんでした。マイクへ近づいてもう一度話してください",
    network: "音声認識サービスへ接続できません。HTTPS接続またはChromeで試してください",
  };
  return messages[code] || "音声認識を開始できませんでした";
}

async function setupRecognition(button, onText) {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) { showToast("この端末では音声入力を利用できません"); return; }
  if (!window.isSecureContext) {
    showToast("音声入力にはHTTPS接続が必要です。文字欄ではGboardのマイク入力を利用できます");
    return;
  }
  state.recognition?.abort();
  if (navigator.mediaDevices?.getUserMedia) {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      stream.getTracks().forEach((track) => track.stop());
    } catch (error) {
      const code = error?.name === "NotAllowedError" ? "not-allowed" : "audio-capture";
      showToast(recognitionErrorMessage(code));
      return;
    }
  }
  const recognition = new SpeechRecognition();
  recognition.lang = "ja-JP";
  recognition.interimResults = false;
  recognition.onstart = () => button.classList.add("listening");
  recognition.onend = () => button.classList.remove("listening");
  recognition.onerror = (event) => {
    if (event.error !== "aborted") showToast(recognitionErrorMessage(event.error));
  };
  recognition.onresult = (event) => onText(event.results[0][0].transcript);
  state.recognition = recognition;
  try {
    recognition.start();
  } catch {
    showToast("音声認識を開始できませんでした。文字欄ではGboardのマイク入力を利用できます");
  }
}

function updateCookVoiceButton() {
  const button = $("#cookVoice");
  const enabled = state.cookVoiceEnabled;
  const starting = state.cookVoiceStarting;
  button.classList.toggle("active", enabled);
  button.classList.toggle("starting", starting);
  button.setAttribute("aria-pressed", String(enabled));
  button.setAttribute("aria-label", `音声操作を${enabled ? "OFF" : "ON"}にする`);
  button.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 15a4 4 0 0 0 4-4V6a4 4 0 1 0-8 0v5a4 4 0 0 0 4 4Z"></path><path d="M19 11a7 7 0 0 1-14 0M12 18v4M8 22h8"></path></svg><span>${starting ? "接続中" : enabled ? "音声 ON" : "音声 OFF"}</span>`;
}

function scheduleCookRecognition(delay = 250) {
  clearTimeout(state.cookVoiceRestartTimer);
  if (!state.cookVoiceEnabled || state.cookVoicePaused || state.cookRecognition
    || document.visibilityState !== "visible" || $("#cookMode").classList.contains("hidden")) return;
  state.cookVoiceRestartTimer = setTimeout(startCookRecognitionSession, delay);
}

function startCookRecognitionSession() {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition || !state.cookVoiceEnabled || state.cookVoicePaused || state.cookRecognition
    || document.visibilityState !== "visible") return;

  const recognition = new SpeechRecognition();
  const startedAt = Date.now();
  recognition.lang = "ja-JP";
  recognition.continuous = true;
  recognition.interimResults = false;
  recognition.onstart = () => {
    $("#cookVoice").classList.add("listening");
    clearTimeout(state.cookVoiceWatchdogTimer);
    state.cookVoiceWatchdogTimer = setTimeout(() => {
      if (state.cookRecognition === recognition) recognition.abort();
    }, 45_000);
  };
  recognition.onresult = (event) => {
    for (let index = event.resultIndex; index < event.results.length; index += 1) {
      if (event.results[index].isFinal) handleCookCommand(event.results[index][0].transcript);
    }
  };
  recognition.onerror = (event) => {
    if (["not-allowed", "service-not-allowed", "audio-capture"].includes(event.error)) {
      state.cookVoiceEnabled = false;
      showToast(recognitionErrorMessage(event.error));
      updateCookVoiceButton();
    } else if (event.error === "network") {
      if (Date.now() - state.cookVoiceLastNetworkNotice > 15_000) {
        state.cookVoiceLastNetworkNotice = Date.now();
        showToast("音声認識へ再接続します");
      }
      setTimeout(() => {
        if (state.cookRecognition === recognition) recognition.abort();
      }, 300);
    } else if (!["aborted", "no-speech"].includes(event.error)) {
      showToast(recognitionErrorMessage(event.error));
    }
  };
  recognition.onend = () => {
    clearTimeout(state.cookVoiceWatchdogTimer);
    if (state.cookRecognition === recognition) state.cookRecognition = null;
    $("#cookVoice").classList.remove("listening");
    state.cookVoiceRetryCount = Date.now() - startedAt < 3_000
      ? Math.min(state.cookVoiceRetryCount + 1, 4)
      : 0;
    scheduleCookRecognition(Math.min(4_000, 300 * (2 ** state.cookVoiceRetryCount)));
  };
  state.cookRecognition = recognition;
  try {
    recognition.start();
  } catch {
    state.cookRecognition = null;
    state.cookVoiceRetryCount = Math.min(state.cookVoiceRetryCount + 1, 4);
    scheduleCookRecognition(Math.min(4_000, 500 * (2 ** state.cookVoiceRetryCount)));
  }
}

async function enableCookVoice() {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) { showToast("この端末では音声入力を利用できません"); return; }
  if (!window.isSecureContext) {
    showToast("音声入力にはHTTPS接続が必要です");
    return;
  }
  if (state.cookVoiceEnabled || state.cookVoiceStarting) return;
  state.cookVoiceStarting = true;
  updateCookVoiceButton();

  if (navigator.mediaDevices?.getUserMedia) {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      stream.getTracks().forEach((track) => track.stop());
    } catch (error) {
      const code = error?.name === "NotAllowedError" ? "not-allowed" : "audio-capture";
      state.cookVoiceStarting = false;
      updateCookVoiceButton();
      showToast(recognitionErrorMessage(code));
      return;
    }
  }
  if (!state.cookVoiceStarting) return;
  if ($("#cookMode").classList.contains("hidden")) {
    state.cookVoiceStarting = false;
    updateCookVoiceButton();
    return;
  }
  state.cookVoiceStarting = false;
  state.cookVoiceEnabled = true;
  state.cookVoicePaused = false;
  state.cookVoiceRetryCount = 0;
  updateCookVoiceButton();
  startCookRecognitionSession();
  showToast("音声操作をONにしました");
}

function disableCookVoice(notify = true) {
  state.cookVoiceEnabled = false;
  state.cookVoicePaused = false;
  state.cookVoiceStarting = false;
  clearTimeout(state.cookVoiceRestartTimer);
  clearTimeout(state.cookVoiceWatchdogTimer);
  clearTimeout(state.cookVoiceSpeechResumeTimer);
  state.cookRecognition?.abort();
  state.cookRecognition = null;
  $("#cookVoice").classList.remove("listening");
  updateCookVoiceButton();
  if (notify) showToast("音声操作をOFFにしました");
}

function toggleCookVoice() {
  if (state.cookVoiceEnabled || state.cookVoiceStarting) disableCookVoice();
  else enableCookVoice();
}

function speak(text) {
  if (!("speechSynthesis" in window)) { showToast("読み上げに対応していません"); return; }
  if (state.cookVoiceEnabled) {
    state.cookVoicePaused = true;
    clearTimeout(state.cookVoiceRestartTimer);
    clearTimeout(state.cookVoiceWatchdogTimer);
    clearTimeout(state.cookVoiceSpeechResumeTimer);
    state.cookRecognition?.abort();
  }
  speechSynthesis.cancel();
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = "ja-JP";
  utterance.rate = 0.92;
  const resumeCookVoice = () => {
    clearTimeout(state.cookVoiceSpeechResumeTimer);
    if (!state.cookVoiceEnabled) return;
    state.cookVoicePaused = false;
    scheduleCookRecognition(500);
  };
  utterance.onend = resumeCookVoice;
  utterance.onerror = resumeCookVoice;
  if (state.cookVoiceEnabled) {
    state.cookVoiceSpeechResumeTimer = setTimeout(
      resumeCookVoice,
      Math.max(8_000, Math.min(60_000, text.length * 350 + 3_000)),
    );
  }
  speechSynthesis.speak(utterance);
}

async function sendCookingAction(operation, speakRequested = false) {
  if (state.cookingPending) throw new Error("工程操作を送信中です");
  const unresolvedKey = `unresolved-action:${state.selected.id}`;
  if (sessionStorage.getItem(unresolvedKey)) {
    throw new Error("前回の操作結果が未確認です。Home Assistant側を確認してください");
  }
  const requestId = newRequestId();
  state.cookingPending = true;
  try {
    const result = await api("/api/cooking/action", {
      method: "POST",
      body: JSON.stringify({
        request_id: requestId,
        recipe_id: state.selected.id,
        operation,
        step_number: state.step + 1,
        speak: speakRequested,
      }),
    });
    state.step = result.step_number - 1;
    renderCookStep();
    if (result.speak && result.speech_output === "browser") speak(result.text);
    return result;
  } catch (error) {
    if (!error.httpStatus || error.data?.status === "outcome_unknown" || error.data?.status === "pending") {
      sessionStorage.setItem(unresolvedKey, requestId);
    }
    throw error;
  } finally {
    state.cookingPending = false;
  }
}

async function startCooking() {
  if (!state.selected?.steps.length) { showToast("工程を登録してください"); return; }
  try {
    state.step = 0;
    $("#cookMode").classList.remove("hidden");
    document.body.style.overflow = "hidden";
    enableCookVoice();
    await sendCookingAction("start");
  } catch (error) { showToast(error.message); }
}

function renderCookStep() {
  const recipe = state.selected;
  $("#cookTitle").textContent = recipe.title;
  $("#stepCounter").textContent = `${state.step + 1} / ${recipe.steps.length} 工程`;
  $("#stepNumber").textContent = state.step + 1;
  $("#stepText").textContent = recipe.steps[state.step];
  $("#stepRail").innerHTML = recipe.steps.map((_, index) => `<button class="rail-step ${index === state.step ? "active" : ""}" data-step="${index}">工程 ${index + 1}</button>`).join("");
  $("#prevStep").disabled = state.step === 0;
  $("#nextStep").textContent = state.step === recipe.steps.length - 1 ? "できあがり" : "次へ";
  sessionStorage.setItem(`step:${recipe.id}`, state.step);
}

async function changeStep(direction) {
  const last = state.selected.steps.length - 1;
  if (direction > 0 && state.step === last) {
    speak("おつかれさまでした。料理のできあがりです。");
    showToast("できあがり！");
    return;
  }
  try { await sendCookingAction(direction > 0 ? "next" : "previous"); }
  catch (error) { showToast(error.message); }
}

function handleCookCommand(text) {
  $("#cookCommandInput").value = text;
  showToast(`「${text}」`);
  if (/次|進んで/.test(text)) changeStep(1);
  else if (/戻|前/.test(text)) changeStep(-1);
  else if (/読|もう一度|繰り返/.test(text)) sendCookingAction("show", true).catch((error) => showToast(error.message));
  else {
    const match = text.match(/(\d+)\s*分/);
    if (match) startTimer(Number(match[1]));
    else if (/停止|止め/.test(text)) stopTimer();
    else showToast("「次へ」「戻って」「読み上げ」「5分タイマー」が使えます");
  }
}

async function sendTimerAction(operation, minutes = 0) {
  if (state.timerPending) throw new Error("タイマー操作を送信中です");
  state.timerPending = true;
  try {
    return await api("/api/timer/action", {
      method: "POST",
      body: JSON.stringify({
        request_id: newRequestId(),
        operation,
        minutes,
      }),
    });
  } finally {
    state.timerPending = false;
  }
}

function resetLocalTimer() {
  clearInterval(state.timer);
  state.timer = null;
  state.timerEnds = 0;
  $("#timerDisplay").textContent = "00:00";
}

async function startTimer(minutes) {
  try {
    const result = await sendTimerAction("start", minutes);
    resetLocalTimer();
    state.timerEnds = Date.now() + minutes * 60_000;
    updateTimer();
    state.timer = setInterval(updateTimer, 250);
    showToast(result.alert_output === "alexa"
      ? `${minutes}分タイマーをAlexaへ送りました`
      : `${minutes}分タイマーを開始しました`);
  } catch (error) {
    showToast(error.message);
  }
}

function updateTimer() {
  const remaining = Math.max(0, Math.ceil((state.timerEnds - Date.now()) / 1000));
  const mins = String(Math.floor(remaining / 60)).padStart(2, "0");
  const secs = String(remaining % 60).padStart(2, "0");
  $("#timerDisplay").textContent = `${mins}:${secs}`;
  if (remaining === 0 && state.timer) {
    resetLocalTimer();
    if (state.runtime?.mode !== "home_assistant") speak("タイマーが終了しました");
    showToast("タイマーが終了しました");
  }
}

async function stopTimer(notify = true) {
  try {
    await sendTimerAction("stop");
    resetLocalTimer();
    if (notify) showToast("タイマーを停止しました");
  } catch (error) {
    showToast(error.message);
  }
}

document.addEventListener("click", (event) => {
  const target = event.target.closest("button, [data-open]");
  if (!target) return;
  if (target.dataset.open) {
    const recipe = state.recipes.find((item) => item.id === target.dataset.open);
    if (recipe) openDetail(recipe);
  }
  if (target.dataset.query != null) { $("#searchInput").value = target.dataset.query; loadRecipes(target.dataset.query); }
  if (target.dataset.serving) { state.servings = adjustServings(state.servings, Number(target.dataset.serving)); renderDetail(); }
  if (target.dataset.step != null) { state.step = Number(target.dataset.step); renderCookStep(); }
  if (target.dataset.minutes) startTimer(Number(target.dataset.minutes));
  if (target.dataset.mode) {
    setMode(target.dataset.mode);
    if (target.dataset.sourceTarget) setSourceFilter(target.dataset.sourceTarget);
  }
  if (target.dataset.source) setSourceFilter(target.dataset.source);
  const actions = {
    home: goHome,
    "open-editor": () => openEditor(null, target.dataset.kind === "video"),
    "close-editor": () => $("#editorDialog").close(),
    "edit-current": () => openEditor(state.selected.read_only ? { ...state.selected, id: "" } : state.selected),
    "sync-mealie": () => syncCurrentRecipe(target),
    "delete-current": deleteCurrentRecipe,
    "refresh-recipes": () => loadRecipes($("#searchInput").value).catch((error) => showToast(error.message)),
    "start-cooking": startCooking,
    "stop-cooking": () => { disableCookVoice(false); $("#cookMode").classList.add("hidden"); document.body.style.overflow = ""; window.speechSynthesis?.cancel(); },
    "timer-stop": () => stopTimer(),
  };
  actions[target.dataset.action]?.();
});

$("#searchForm").addEventListener("submit", (event) => { event.preventDefault(); loadRecipes($("#searchInput").value); });
$("#searchInput").addEventListener("input", (event) => { clearTimeout(state.searchTimer); state.searchTimer = setTimeout(() => loadRecipes(event.target.value), 250); });
$("#voiceSearch").addEventListener("click", () => setupRecognition($("#voiceSearch"), (text) => { $("#searchInput").value = text; loadRecipes(text); }));
$("#cookVoice").addEventListener("click", toggleCookVoice);
document.addEventListener("visibilitychange", () => {
  if (!state.cookVoiceEnabled) return;
  if (document.visibilityState === "hidden") {
    clearTimeout(state.cookVoiceRestartTimer);
    clearTimeout(state.cookVoiceWatchdogTimer);
    state.cookRecognition?.abort();
  } else {
    scheduleCookRecognition(200);
  }
});
window.addEventListener("focus", () => scheduleCookRecognition(200));
$("#cookCommandForm").addEventListener("submit", (event) => {
  event.preventDefault();
  const input = $("#cookCommandInput");
  const command = input.value.trim();
  if (!command) return;
  handleCookCommand(command);
});
$("#recipeForm").addEventListener("submit", saveRecipe);
$("#importForm").addEventListener("submit", importFromUrl);
$("#videoImportForm").addEventListener("submit", importFromVideo);
$("#prevStep").addEventListener("click", () => changeStep(-1));
$("#nextStep").addEventListener("click", () => changeStep(1));
$("#speakStep").addEventListener("click", () => sendCookingAction("show", true).catch((error) => showToast(error.message)));

function registerWebMcpTools() {
  const context = document.modelContext;
  if (!context?.registerTool) return;
  const registration = (tool) => Promise.resolve(context.registerTool(tool)).catch(() => {});
  registration({
    name: "search_recipes",
    title: "レシピを検索",
    description: "登録済みレシピを料理名、材料、タグで検索します。",
    inputSchema: {
      type: "object",
      properties: { query: { type: "string", maxLength: 160 } },
      required: ["query"],
      additionalProperties: false,
    },
    annotations: { readOnlyHint: true, untrustedContentHint: true },
    async execute(input) {
      if (!input || typeof input.query !== "string") throw new Error("queryが必要です");
      const result = await api(`/api/catalog/recipes?q=${encodeURIComponent(input.query)}`);
      return { recipes: result.recipes.map(({ id, title, servings, tags }) => ({ id, title, servings, tags })) };
    },
  });
  registration({
    name: "create_recipe",
    title: "レシピを登録",
    description: "材料と工程を含むレシピを保存し、画面のレシピ一覧を更新します。",
    inputSchema: {
      type: "object",
      properties: {
        title: { type: "string", minLength: 1, maxLength: 160 },
        servings: { type: "number", exclusiveMinimum: 0, maximum: 100 },
        ingredients: { type: "array", items: { type: "string" }, maxItems: 200 },
        steps: { type: "array", items: { type: "string" }, maxItems: 100 },
        source_url: { type: "string" },
      },
      required: ["title", "servings", "ingredients", "steps"],
      additionalProperties: false,
    },
    annotations: { readOnlyHint: false, untrustedContentHint: true },
    async execute(input) {
      if (!input || typeof input.title !== "string" || !Array.isArray(input.ingredients) || !Array.isArray(input.steps)) {
        throw new Error("レシピの入力形式が正しくありません");
      }
      const recipe = await api("/api/recipes", {
        method: "POST",
        body: JSON.stringify({
          title: input.title,
          servings: input.servings,
          ingredients: input.ingredients,
          steps: input.steps,
          source_url: input.source_url || "",
          tags: [],
        }),
      });
      await loadRecipes();
      return { id: recipe.id, title: recipe.title, saved: true };
    },
  });
}

registerWebMcpTools();
Promise.all([loadRuntime(), loadRecipes()]).catch((error) => {
  $("#recipeGrid").innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
});
