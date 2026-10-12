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
  cookRecognitions: new Set(),
  cookVoiceEnabled: false,
  cookVoicePaused: false,
  cookVoiceRestartTimer: null,
  cookVoiceWatchdogTimer: null,
  cookVoiceSpeechResumeTimer: null,
  cookVoiceRetryCount: 0,
  cookVoiceStarting: false,
  cookVoiceGeneration: 0,
  cookVoiceLastNetworkNotice: 0,
  editorImage: "",
  runtime: null,
  activeMode: "assist",
  sourceFilter: "all",
  catalogStatus: null,
  quantityMode: "recipe",
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

function videoEmbedUrl(value = "", startSeconds = 0) {
  const url = safeUrl(value);
  if (!url) return "";
  const parsed = new URL(url);
  const hostname = parsed.hostname.toLowerCase();
  let videoId = "";
  if (hostname === "youtu.be") videoId = parsed.pathname.split("/").filter(Boolean)[0] || "";
  if (["youtube.com", "www.youtube.com", "m.youtube.com", "www.youtube-nocookie.com"].includes(hostname)) {
    videoId = parsed.searchParams.get("v") || "";
    if (!videoId) {
      const parts = parsed.pathname.split("/").filter(Boolean);
      if (["embed", "shorts", "live"].includes(parts[0])) videoId = parts[1] || "";
    }
  }
  if (/^[A-Za-z0-9_-]{6,20}$/.test(videoId)) {
    const start = Math.max(0, Math.floor(Number(startSeconds) || 0));
    return `https://www.youtube-nocookie.com/embed/${videoId}${start ? `?start=${start}` : ""}`;
  }
  if (["vimeo.com", "www.vimeo.com", "player.vimeo.com"].includes(hostname)) {
    const videoNumber = parsed.pathname.split("/").filter(Boolean).find((part) => /^\d+$/.test(part));
    if (videoNumber) {
      const start = Math.max(0, Math.floor(Number(startSeconds) || 0));
      return `https://player.vimeo.com/video/${videoNumber}${start ? `#t=${start}s` : ""}`;
    }
  }
  return "";
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

function formatVideoTimestamp(value) {
  if (value == null || !Number.isFinite(Number(value))) return "";
  const total = Math.max(0, Math.floor(Number(value)));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = String(total % 60).padStart(2, "0");
  return hours
    ? `${hours}:${String(minutes).padStart(2, "0")}:${seconds}`
    : `${String(minutes).padStart(2, "0")}:${seconds}`;
}

function scaledIngredient(item, base, target, canScale = true) {
  if (item.amount == null) return item.note || "適量";
  if (!canScale) return `${formatNumber(item.amount)}${item.unit ? ` ${item.unit}` : ""}`;
  return `${formatNumber(Number(item.amount) * target / base)}${item.unit ? ` ${item.unit}` : ""}`;
}

function positiveNumber(value, fallback) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : fallback;
}

function portionGuidance(recipe, servings = state.servings) {
  const kind = recipe.portion_kind || "standard";
  const people = positiveNumber(servings, 1);
  const guidance = [];
  if (["meat_main", "mixed_meat"].includes(kind)) {
    const defaultGrams = kind === "meat_main" ? 200 : 150;
    const grams = positiveNumber(recipe.meat_g_per_person, defaultGrams) * people;
    guidance.push({ name: "肉の適量目安", amount: `${formatNumber(grams)} g` });
  }
  if (["drink", "diluted_drink"].includes(kind)) {
    const total = positiveNumber(recipe.serving_volume_ml, 300) * people;
    if (kind === "diluted_drink") {
      const concentrate = positiveNumber(recipe.dilution_concentrate_parts, 3);
      const mixer = positiveNumber(recipe.dilution_mixer_parts, 7);
      const allParts = concentrate + mixer;
      guidance.push({
        name: "濃縮液の適量目安",
        amount: `${formatNumber(total * concentrate / allParts)} ml`,
      });
      guidance.push({
        name: "水・炭酸水の適量目安",
        amount: `${formatNumber(total * mixer / allParts)} ml`,
      });
    } else {
      guidance.push({ name: "ドリンク完成量の目安", amount: `${formatNumber(total)} ml` });
    }
  }
  const rice = Number(recipe.rice_go_per_person || 0) * people;
  if (rice > 0) guidance.push({ name: "ご飯の適量目安", amount: `${formatNumber(rice)} 合` });
  return guidance;
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
  state.quantityMode = "recipe";
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
  const embeddedVideo = videoEmbedUrl(recipe.video_url);
  const servingsConfirmed = recipe.servings_confirmed !== false;
  const guidance = portionGuidance(recipe);
  if (!guidance.length) state.quantityMode = "recipe";
  const showingGuidance = state.quantityMode === "guidance";
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
        <div class="serving-control">${servingsConfirmed ? `<button data-serving="-1" aria-label="人数を減らす">−</button><strong>${formatServings(state.servings)}人分</strong>` : "<strong>人数・換算は未確認</strong>"}</div>
        <div class="quantity-mode" aria-label="分量の表示方法">
          <button class="${showingGuidance ? "" : "active"}" data-quantity-mode="recipe" aria-pressed="${!showingGuidance}">メニューの分量</button>
          <button class="${showingGuidance ? "active" : ""}" data-quantity-mode="guidance" aria-pressed="${showingGuidance}" ${guidance.length ? "" : "disabled"}>1人分の適量</button>
        </div>
        <div class="detail-actions"><button class="primary-button" data-action="start-cooking">調理をはじめる</button><button class="secondary-button" data-action="edit-current">${recipe.read_only ? "下書きにコピー" : "編集する"}</button>${syncButton}${deleteControl}</div>
      </div>
    </div>
    ${embeddedVideo ? `<section class="video-panel"><div class="video-frame"><iframe src="${escapeHtml(embeddedVideo)}" title="${escapeHtml(recipe.title)}の料理動画" loading="lazy" referrerpolicy="strict-origin-when-cross-origin" sandbox="allow-scripts allow-same-origin allow-presentation" allow="encrypted-media; picture-in-picture; fullscreen" allowfullscreen></iframe></div><a class="source-link" href="${escapeHtml(video)}" target="_blank" rel="noopener noreferrer">動画を別画面で開く</a></section>` : ""}
    <div class="recipe-columns"><section><p class="eyebrow">${showingGuidance ? "PORTION GUIDE" : "INGREDIENTS"}</p><h2>${showingGuidance ? "1人分の適量から計算" : "メニュー（レシピ）の分量"}</h2>
      ${showingGuidance ? '<p class="quantity-note">一般的な適量の目安です。レシピに記載された分量とは別に表示しています。</p>' : ""}
      <ul class="ingredient-list">
      ${(showingGuidance ? guidance : recipe.ingredients.map((item) => ({ ...item, amount_label: scaledIngredient(item, recipe.servings, state.servings, servingsConfirmed) }))).map((item) => `<li><span>${escapeHtml(item.name)}${item.note ? `<small> ${escapeHtml(item.note)}</small>` : ""}</span><strong>${escapeHtml(item.amount_label || item.amount)}</strong></li>`).join("")}
      </ul>${source ? `<a class="source-link" href="${escapeHtml(source)}" target="_blank" rel="noopener noreferrer">出典ページを開く</a>` : ""}${video && !embeddedVideo ? `<br><a class="source-link" href="${escapeHtml(video)}" target="_blank" rel="noopener noreferrer">動画を開く</a>` : ""}</section>
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

function updatePortionFields(applyDefaults = false) {
  const kind = $("#portionKindField").value;
  const hasMeat = ["meat_main", "mixed_meat"].includes(kind);
  const hasDrink = ["drink", "diluted_drink"].includes(kind);
  const hasDilution = kind === "diluted_drink";
  $$('[data-portion-field="meat"]').forEach((field) => field.classList.toggle("hidden", !hasMeat));
  $$('[data-portion-field="drink"]').forEach((field) => field.classList.toggle("hidden", !hasDrink));
  $$('[data-portion-field="dilution"]').forEach((field) => field.classList.toggle("hidden", !hasDilution));
  if (!applyDefaults) return;
  if (hasMeat) $("#meatPerPersonField").value = kind === "meat_main" ? 200 : 150;
  if (hasDrink) $("#servingVolumeField").value = 300;
  if (hasDilution) {
    $("#concentratePartsField").value = 3;
    $("#mixerPartsField").value = 7;
  }
}

function openEditor(recipe = null, videoOnly = false) {
  state.editorImage = recipe?.image_url || "";
  $("#recipeId").value = recipe?.id || "";
  $("#editorTitle").textContent = videoOnly ? "動画レシピを登録" : recipe ? "レシピを編集" : "レシピを登録";
  $("#titleField").value = recipe?.title || "";
  $("#servingsField").value = recipe?.servings || 2;
  $("#portionKindField").value = recipe?.portion_kind || "standard";
  $("#meatPerPersonField").value = recipe?.meat_g_per_person ?? 200;
  $("#servingVolumeField").value = recipe?.serving_volume_ml ?? 300;
  $("#concentratePartsField").value = recipe?.dilution_concentrate_parts ?? 3;
  $("#mixerPartsField").value = recipe?.dilution_mixer_parts ?? 7;
  $("#ricePerPersonField").value = recipe?.rice_go_per_person ?? 0;
  $("#tagsField").value = recipe?.tags?.join(", ") || "";
  $("#descriptionField").value = recipe?.description || "";
  $("#ingredientsField").value = recipe?.ingredients?.map((item) => item.amount == null ? `${item.name}${item.note ? `（${item.note}）` : ""}` : `${formatNumber(item.amount)} | ${item.unit} | ${item.name}`).join("\n") || "";
  $("#stepsField").value = recipe?.steps?.join("\n") || "";
  $("#stepTimestampsField").value = recipe?.steps?.map((_, index) => (
    formatVideoTimestamp(recipe?.step_video_timestamps?.[index])
  )).join("\n") || "";
  $("#sourceField").value = recipe?.source_url || "";
  $("#videoField").value = recipe?.video_url || "";
  $("#formMessage").textContent = "";
  updatePortionFields();
  $("#editorDialog").showModal();
  setTimeout(() => (videoOnly ? $("#videoField") : $("#titleField")).focus(), 50);
}

async function saveRecipe(event) {
  event.preventDefault();
  const id = $("#recipeId").value;
  const payload = {
    title: $("#titleField").value,
    servings: Number($("#servingsField").value),
    portion_kind: $("#portionKindField").value,
    meat_g_per_person: Number($("#meatPerPersonField").value),
    serving_volume_ml: Number($("#servingVolumeField").value),
    dilution_concentrate_parts: Number($("#concentratePartsField").value),
    dilution_mixer_parts: Number($("#mixerPartsField").value),
    rice_go_per_person: Number($("#ricePerPersonField").value),
    tags: $("#tagsField").value.split(",").map((tag) => tag.trim()).filter(Boolean),
    description: $("#descriptionField").value,
    ingredients: $("#ingredientsField").value.split("\n").map((line) => line.trim()).filter(Boolean).map(parseIngredientLine),
    steps: $("#stepsField").value.split("\n").map((line) => line.trim()).filter(Boolean),
    step_video_timestamps: $("#stepTimestampsField").value
      .split("\n").map((line) => line.trim()),
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
  const generation = state.cookVoiceGeneration;
  state.cookVoiceRestartTimer = setTimeout(() => {
    state.cookVoiceRestartTimer = null;
    if (generation === state.cookVoiceGeneration) startCookRecognitionSession();
  }, delay);
}

function startCookRecognitionSession() {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition || !state.cookVoiceEnabled || state.cookVoicePaused || state.cookRecognition
    || document.visibilityState !== "visible") return;

  const generation = state.cookVoiceGeneration;
  const recognition = new SpeechRecognition();
  state.cookRecognitions.add(recognition);
  const startedAt = Date.now();
  const isCurrent = () => generation === state.cookVoiceGeneration
    && state.cookVoiceEnabled && state.cookRecognition === recognition;
  recognition.lang = "ja-JP";
  recognition.continuous = false;
  recognition.interimResults = false;
  recognition.onstart = () => {
    if (!isCurrent()) {
      recognition.abort();
      return;
    }
    $("#cookVoice").classList.add("listening");
    clearTimeout(state.cookVoiceWatchdogTimer);
    state.cookVoiceWatchdogTimer = setTimeout(() => {
      if (state.cookRecognition === recognition) recognition.abort();
    }, 45_000);
  };
  recognition.onresult = (event) => {
    if (!isCurrent()) return;
    for (let index = event.resultIndex; index < event.results.length; index += 1) {
      if (event.results[index].isFinal) handleCookCommand(event.results[index][0].transcript);
    }
  };
  recognition.onerror = (event) => {
    if (generation !== state.cookVoiceGeneration) return;
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
    state.cookRecognitions.delete(recognition);
    clearTimeout(state.cookVoiceWatchdogTimer);
    if (state.cookRecognition === recognition) state.cookRecognition = null;
    $("#cookVoice").classList.remove("listening");
    if (generation !== state.cookVoiceGeneration || !state.cookVoiceEnabled) return;
    state.cookVoiceRetryCount = Date.now() - startedAt < 3_000
      ? Math.min(state.cookVoiceRetryCount + 1, 4)
      : 0;
    scheduleCookRecognition(Math.min(4_000, 300 * (2 ** state.cookVoiceRetryCount)));
  };
  state.cookRecognition = recognition;
  try {
    recognition.start();
  } catch {
    state.cookRecognitions.delete(recognition);
    state.cookRecognition = null;
    state.cookVoiceRetryCount = Math.min(state.cookVoiceRetryCount + 1, 4);
    scheduleCookRecognition(Math.min(4_000, 500 * (2 ** state.cookVoiceRetryCount)));
  }
}

async function enableCookVoice(announce = true) {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) { showToast("この端末では音声入力を利用できません"); return; }
  if (!window.isSecureContext) {
    showToast("音声入力にはHTTPS接続が必要です");
    return;
  }
  if (state.cookVoiceEnabled || state.cookVoiceStarting) return;
  const generation = state.cookVoiceGeneration + 1;
  state.cookVoiceGeneration = generation;
  state.cookVoiceStarting = true;
  updateCookVoiceButton();

  if (navigator.mediaDevices?.getUserMedia) {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      stream.getTracks().forEach((track) => track.stop());
    } catch (error) {
      if (generation !== state.cookVoiceGeneration) return;
      const code = error?.name === "NotAllowedError" ? "not-allowed" : "audio-capture";
      state.cookVoiceStarting = false;
      updateCookVoiceButton();
      showToast(recognitionErrorMessage(code));
      return;
    }
  }
  if (!state.cookVoiceStarting || generation !== state.cookVoiceGeneration) return;
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
  if (announce) announceCurrentCookStage();
}

function disableCookVoice(notify = true) {
  state.cookVoiceGeneration += 1;
  state.cookVoiceEnabled = false;
  state.cookVoicePaused = false;
  state.cookVoiceStarting = false;
  clearTimeout(state.cookVoiceRestartTimer);
  clearTimeout(state.cookVoiceWatchdogTimer);
  clearTimeout(state.cookVoiceSpeechResumeTimer);
  state.cookVoiceRestartTimer = null;
  state.cookVoiceWatchdogTimer = null;
  state.cookVoiceSpeechResumeTimer = null;
  state.cookRecognition = null;
  $("#cookVoice").classList.remove("listening");
  updateCookVoiceButton();
  window.speechSynthesis?.cancel();
  for (const recognition of state.cookRecognitions) {
    recognition.onstart = null;
    recognition.onresult = null;
    recognition.onerror = null;
    recognition.onend = null;
    try { recognition.abort(); } catch { /* 既に終了している場合は停止済み */ }
  }
  state.cookRecognitions.clear();
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

function speakOnConfiguredOutput(text) {
  if (state.runtime?.speech === "browser") speak(text);
}

function pauseCookRecognitionForRemoteSpeech(text) {
  if (!state.cookVoiceEnabled) return;
  state.cookVoicePaused = true;
  clearTimeout(state.cookVoiceRestartTimer);
  clearTimeout(state.cookVoiceWatchdogTimer);
  clearTimeout(state.cookVoiceSpeechResumeTimer);
  state.cookRecognition?.abort();
  state.cookVoiceSpeechResumeTimer = setTimeout(() => {
    if (!state.cookVoiceEnabled) return;
    state.cookVoicePaused = false;
    scheduleCookRecognition(300);
  }, Math.max(8_000, Math.min(60_000, text.length * 350 + 5_000)));
}

function hasPreparationStage() {
  if (!state.selected) return false;
  if (state.quantityMode === "guidance") return Boolean(portionGuidance(state.selected).length);
  return Boolean(state.selected.ingredients?.length);
}

function preparationItems() {
  const recipe = state.selected;
  if (state.quantityMode === "guidance") {
    return portionGuidance(recipe).map((item) => ({
      name: item.name,
      note: "一般的な1人分の適量から計算",
      amount: item.amount,
    }));
  }
  const canScale = recipe.servings_confirmed !== false;
  return recipe.ingredients.map((item) => ({
    name: item.name,
    note: item.note || "",
    amount: scaledIngredient(item, recipe.servings, state.servings, canScale),
  }));
}

function preparationSpeech() {
  const items = preparationItems().map((item) => {
    const note = item.note ? `、${item.note}` : "";
    return `${item.name}、${item.amount}${note}`;
  });
  return `まず、材料を準備します。${items.join("。")}。準備できたら、できました、と言ってください。`;
}

function announceCurrentCookStage() {
  if (!state.cookVoiceEnabled || !state.selected) return;
  if (state.step === -1) {
    speakOnConfiguredOutput(preparationSpeech());
    return;
  }
  sendCookingAction("show", true).catch((error) => showToast(error.message));
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
        step_number: Math.max(1, state.step + 1),
        speak: speakRequested,
      }),
    });
    state.step = result.step_number - 1;
    renderCookStep();
    if (result.speak && result.speech_output === "browser") speak(result.text);
    if (result.speak && result.speech_output === "home_assistant") {
      pauseCookRecognitionForRemoteSpeech(result.text);
    }
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
    disableCookVoice(false);
    state.step = hasPreparationStage() ? -1 : 0;
    $("#cookMode").classList.remove("hidden");
    document.body.style.overflow = "hidden";
    renderCookStep();
    if (state.step !== -1) await sendCookingAction("start", false);
  } catch (error) { showToast(error.message); }
}

function renderCookStep() {
  const recipe = state.selected;
  const preparing = state.step === -1;
  const hasPreparation = hasPreparationStage();
  const totalStages = recipe.steps.length + (hasPreparation ? 1 : 0);
  const currentStage = state.step + (hasPreparation ? 2 : 1);
  $("#cookTitle").textContent = recipe.title;
  $("#stepCounter").textContent = `${currentStage} / ${totalStages} ステージ`;
  $("#stepNumber").classList.toggle("preparation", preparing);
  $("#stepNumber").textContent = preparing ? "準備" : state.step + 1;
  $("#stepText").textContent = preparing ? "材料を準備しましょう" : recipe.steps[state.step];
  const videoStart = preparing ? 0 : recipe.step_video_timestamps?.[state.step] || 0;
  const cookVideoUrl = videoEmbedUrl(recipe.video_url, videoStart);
  $("#cookVideo").classList.toggle("hidden", !cookVideoUrl);
  $("#cookVideo").innerHTML = cookVideoUrl
    ? `<div class="cook-video-frame"><iframe src="${escapeHtml(cookVideoUrl)}" title="${escapeHtml(recipe.title)} ${preparing ? "準備" : `工程${state.step + 1}`}の動画" loading="lazy" referrerpolicy="strict-origin-when-cross-origin" sandbox="allow-scripts allow-same-origin allow-presentation" allow="encrypted-media; picture-in-picture; fullscreen" allowfullscreen></iframe></div>${videoStart ? `<small>${escapeHtml(formatVideoTimestamp(videoStart))}から表示</small>` : ""}`
    : "";
  $("#preparationList").classList.toggle("hidden", !preparing);
  $("#preparationList").innerHTML = preparing
    ? preparationItems().map((item) => `<li><span>${escapeHtml(item.name)}${item.note ? `<small>${escapeHtml(item.note)}</small>` : ""}</span><strong>${escapeHtml(item.amount)}</strong></li>`).join("")
    : "";
  const preparationRail = hasPreparation
    ? `<button class="rail-step ${preparing ? "active" : ""}" data-step="-1">材料準備</button>`
    : "";
  $("#stepRail").innerHTML = preparationRail + recipe.steps.map((_, index) => `<button class="rail-step ${index === state.step ? "active" : ""}" data-step="${index}">工程 ${index + 1}</button>`).join("");
  $("#prevStep").disabled = preparing || (!hasPreparation && state.step === 0);
  $("#nextStep").textContent = preparing
    ? "準備できました"
    : state.step === recipe.steps.length - 1 ? "できあがり" : "できました";
  sessionStorage.setItem(`step:${recipe.id}`, state.step);
}

async function changeStep(direction) {
  if (direction > 0 && state.step === -1) {
    try { await sendCookingAction("start", state.cookVoiceEnabled); }
    catch (error) { showToast(error.message); }
    return;
  }
  if (direction < 0 && state.step === 0 && hasPreparationStage()) {
    state.step = -1;
    renderCookStep();
    if (state.cookVoiceEnabled) speakOnConfiguredOutput(preparationSpeech());
    return;
  }
  const last = state.selected.steps.length - 1;
  if (direction > 0 && state.step === last) {
    speakOnConfiguredOutput("おつかれさまでした。料理のできあがりです。");
    showToast("できあがり！");
    return;
  }
  try { await sendCookingAction(direction > 0 ? "next" : "previous", state.cookVoiceEnabled); }
  catch (error) { showToast(error.message); }
}

async function selectCookStage(step) {
  if (step === -1 && hasPreparationStage()) {
    state.step = -1;
    renderCookStep();
    if (state.cookVoiceEnabled) speakOnConfiguredOutput(preparationSpeech());
    return;
  }
  if (step < 0 || step >= state.selected.steps.length) return;
  state.step = step;
  renderCookStep();
  try { await sendCookingAction("show", state.cookVoiceEnabled); }
  catch (error) { showToast(error.message); }
}

function handleCookCommand(text) {
  $("#cookCommandInput").value = text;
  showToast(`「${text}」`);
  if (/次|進んで|できました|できた|完了/.test(text)) changeStep(1);
  else if (/戻|前/.test(text)) changeStep(-1);
  else if (/読|もう一度|繰り返/.test(text)) sendCookingAction("show", true).catch((error) => showToast(error.message));
  else {
    const match = text.match(/(\d+)\s*分/);
    if (match) startTimer(Number(match[1]));
    else if (/停止|止め/.test(text)) stopTimer();
    else showToast("「できました」「戻って」「読み上げ」「5分タイマー」が使えます");
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
  if (target.dataset.quantityMode) {
    state.quantityMode = target.dataset.quantityMode;
    renderDetail();
  }
  if (target.dataset.step != null) selectCookStage(Number(target.dataset.step));
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
$("#portionKindField").addEventListener("change", () => updatePortionFields(true));
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
