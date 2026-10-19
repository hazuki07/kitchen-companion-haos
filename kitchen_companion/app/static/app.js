const state = {
  recipes: [],
  selected: null,
  multiplier: 1,
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
  bundleImport: null,
  youtubeReimport: null,
  youtubeReimportDrafts: new Map(),
  youtubeReimportSummary: null,
  youtubeReimportRunning: false,
  quantityMode: "recipe",
  cookTab: "step",
  shoppingSuggestions: [],
  shoppingSelectedId: "",
  shoppingMultiplier: 1,
  pantryIngredients: new Set(),
  listOutcomeUnknown: new Set(),
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

function adjustMultiplier(current, direction) {
  const value = Number(current) || 1;
  return Math.min(10, Math.max(0.5, Math.round((value + direction * 0.5) * 2) / 2));
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

function scaledIngredient(item, multiplier = 1) {
  if (item.amount == null) {
    const qualitativeAmounts = new Set(["適量", "少々", "多めの少々", "ひとつまみ"]);
    const note = String(item.note || "").trim();
    return qualitativeAmounts.has(note) ? note : "未確認";
  }
  const ratio = positiveNumber(multiplier, 1);
  const minimum = formatNumber(Number(item.amount) * ratio);
  const maximum = item.amount_max == null ? "" : formatNumber(Number(item.amount_max) * ratio);
  const amount = maximum ? `${minimum}〜${maximum}` : minimum;
  return `${amount}${item.unit ? ` ${item.unit}` : ""}`;
}

function positiveNumber(value, fallback) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : fallback;
}

const SHOPPING_MOODS = [
  { triggers: ["がっつり", "ボリューム", "お腹いっぱい", "元気", "スタミナ", "こってり"], terms: ["肉", "牛", "豚", "鶏", "揚げ", "丼", "カレー"] },
  { triggers: ["さっぱり", "あっさり", "暑い", "食欲がない"], terms: ["魚", "野菜", "サラダ", "酢", "レモン", "冷", "和風"] },
  { triggers: ["温かい", "あたたかい", "暖かい", "寒い"], terms: ["スープ", "鍋", "煮", "汁", "シチュー", "麺"] },
  { triggers: ["野菜", "ヘルシー", "軽い"], terms: ["野菜", "サラダ", "にんじん", "玉ねぎ", "キャベツ", "トマト"] },
  { triggers: ["手軽", "簡単", "早い", "時短", "疲れた", "楽したい", "忙しい"], terms: ["簡単", "時短", "20分", "炒め", "丼"] },
  { triggers: ["辛い", "スパイシー"], terms: ["辛", "カレー", "唐辛子", "キムチ", "麻婆"] },
];

function normalizedShoppingText(value) {
  return String(value || "").normalize("NFKC").toLowerCase().replace(/\s+/g, "");
}

const PANTRY_STORAGE_KEY = "kitchen-companion:pantry-ingredients";

function loadPantryIngredients() {
  try {
    const values = JSON.parse(localStorage.getItem(PANTRY_STORAGE_KEY) || "[]");
    return new Set(Array.isArray(values) ? values.filter((value) => typeof value === "string") : []);
  } catch {
    return new Set();
  }
}

function savePantryIngredients() {
  try {
    localStorage.setItem(PANTRY_STORAGE_KEY, JSON.stringify([...state.pantryIngredients].sort()));
  } catch {
    showToast("在庫チェックを端末へ保存できませんでした");
  }
}

function shoppingIngredientKey(item) {
  return normalizedShoppingText(item.name);
}

state.pantryIngredients = loadPantryIngredients();

function shoppingTerms(mood) {
  const normalized = normalizedShoppingText(mood);
  const terms = new Set(
    String(mood || "").normalize("NFKC").toLowerCase().split(/[\s、。・,]+/).filter((term) => term.length >= 2),
  );
  SHOPPING_MOODS.forEach((group) => {
    if (group.triggers.some((trigger) => normalized.includes(trigger))) {
      group.terms.forEach((term) => terms.add(term));
    }
  });
  return [...terms].map(normalizedShoppingText).filter(Boolean);
}

function rankShoppingRecipes(recipes, mood) {
  const terms = shoppingTerms(mood);
  return recipes
    .filter((recipe) => recipe.ingredients?.length)
    .map((recipe) => {
      const title = normalizedShoppingText(recipe.title);
      const description = normalizedShoppingText(recipe.description);
      const tags = (recipe.tags || []).map(normalizedShoppingText);
      const ingredients = (recipe.ingredients || []).map((item) => normalizedShoppingText(item.name));
      let score = 0;
      const reasons = [];
      terms.forEach((term) => {
        if (title.includes(term)) { score += 8; reasons.push(`料理名：${term}`); }
        if (tags.some((tag) => tag.includes(term))) { score += 5; reasons.push(`タグ：${term}`); }
        if (ingredients.some((name) => name.includes(term))) { score += 4; reasons.push(`材料：${term}`); }
        if (description.includes(term)) { score += 2; reasons.push(`特徴：${term}`); }
      });
      return { recipe, score, reasons: [...new Set(reasons)].slice(0, 3) };
    })
    .filter((item) => item.score > 0)
    .sort((left, right) => right.score - left.score || left.recipe.title.localeCompare(right.recipe.title, "ja"))
    .slice(0, 5);
}

function selectedShoppingRecipe() {
  return state.shoppingSuggestions.find((item) => item.recipe.id === state.shoppingSelectedId)?.recipe || null;
}

function renderShoppingSuggestions() {
  const element = $("#shoppingSuggestions");
  if (!state.shoppingSuggestions.length) {
    element.innerHTML = '<div class="empty-state"><strong>近いレシピが見つかりませんでした</strong><p>「鶏肉」「温かい」「簡単」など、別の言葉でも試してください。</p></div>';
    renderShoppingList();
    return;
  }
  element.innerHTML = state.shoppingSuggestions.map(({ recipe, reasons }, index) => `
    <article class="shopping-suggestion ${recipe.id === state.shoppingSelectedId ? "selected" : ""}">
      <div><h3>${index + 1}. ${escapeHtml(recipe.title)}</h3><p>${escapeHtml(recipe.description || "保存済みレシピから提案しました")}</p></div>
      <div class="tags">${(reasons.length ? reasons : ["保存済みレシピ"]).map((reason) => `<span class="tag">${escapeHtml(reason)}</span>`).join("")}</div>
      <button class="secondary-button" data-shopping-select="${escapeHtml(recipe.id)}">材料を見る</button>
    </article>`).join("");
  renderShoppingList();
}

function renderShoppingList() {
  const element = $("#shoppingList");
  const recipe = selectedShoppingRecipe();
  if (!recipe) {
    element.innerHTML = '<p class="eyebrow">SHOPPING LIST</p><h2>必要な材料</h2><p>候補を選ぶと買い物リストを表示します。</p>';
    return;
  }
  const remaining = recipe.ingredients.filter((item) => !state.pantryIngredients.has(shoppingIngredientKey(item)));
  const ingredients = recipe.ingredients.map((item, index) => {
    const key = shoppingIngredientKey(item);
    const checked = state.pantryIngredients.has(key);
    return `<li class="${checked ? "checked" : ""}"><input type="checkbox" id="shoppingItem${index}" data-pantry-key="${escapeHtml(key)}" ${checked ? "checked" : ""}><label for="shoppingItem${index}">${escapeHtml(item.name)}</label><strong>${escapeHtml(scaledIngredient(item, state.shoppingMultiplier))}</strong></li>`;
  }).join("");
  element.innerHTML = `
    <p class="eyebrow">SHOPPING LIST</p>
    <div class="shopping-list-heading"><h2>${escapeHtml(recipe.title)}</h2>
      <div class="shopping-multiplier"><button data-shopping-multiplier="-1" aria-label="倍率を下げる">−</button><strong>${formatNumber(state.shoppingMultiplier)}倍</strong><button data-shopping-multiplier="1" aria-label="倍率を上げる">＋</button></div>
    </div>
    <p>レシピの基準量を1倍として換算しています。家にある材料はチェックしてください。チェックはこの端末に保存されます。</p>
    <strong class="shopping-remaining">買い足し ${remaining.length}件</strong>
    <ul class="shopping-ingredients">${ingredients}</ul>
    <div class="shopping-list-actions"><button class="primary-button" data-action="announce-shopping">買い足す材料を読み上げる</button><button class="secondary-button" data-action="reset-shopping-pantry">チェックをリセット</button><button class="secondary-button" data-open="${escapeHtml(recipe.id)}">レシピを見る</button></div>`;
}

function announceShoppingList() {
  const recipe = selectedShoppingRecipe();
  if (!recipe) return;
  const ingredients = recipe.ingredients
    .filter((item) => !state.pantryIngredients.has(shoppingIngredientKey(item)))
    .map((item) => `${item.name}、${scaledIngredient(item, state.shoppingMultiplier)}`);
  speak(ingredients.length
    ? `${recipe.title}で買い足す材料です。${ingredients.join("。")}。`
    : `${recipe.title}で買い足す材料はありません。`);
}

function resetShoppingPantry() {
  const recipe = selectedShoppingRecipe();
  if (!recipe) return;
  recipe.ingredients.forEach((item) => state.pantryIngredients.delete(shoppingIngredientKey(item)));
  savePantryIngredients();
  renderShoppingList();
  showToast("このレシピのチェックをリセットしました");
}

async function recommendShoppingRecipes(mood) {
  const value = String(mood || "").trim();
  if (!value) return;
  const result = await api("/api/catalog/recipes");
  state.recipes = result.recipes;
  state.catalogStatus = result;
  $("#searchInput").value = "";
  state.shoppingSuggestions = rankShoppingRecipes(state.recipes, value);
  state.shoppingSelectedId = state.shoppingSuggestions[0]?.recipe.id || "";
  state.shoppingMultiplier = 1;
  renderGrid();
  renderCatalogStatus();
  renderShoppingSuggestions();
  showToast(state.shoppingSuggestions.length ? `${state.shoppingSuggestions.length}件を提案しました` : "近いレシピが見つかりませんでした");
}

function portionGuidance(recipe, multiplier = state.multiplier) {
  if (recipe.servings_confirmed === false) return [];
  const kind = recipe.portion_kind || "standard";
  const people = positiveNumber(recipe.servings, 1) * positiveNumber(multiplier, 1);
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
  const jevLabel = state.runtime.jev_review_enabled ? " · Jev検品" : "";
  $("#runtimeStatus").innerHTML = `<span></span> ${escapeHtml(state.runtime.label + jevLabel)}`;
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
    const servingLabel = "基準量（1倍）";
    const sourceLabel = ["mealie", "mealie_mock"].includes(recipe.source_kind)
      ? (recipe.source_kind === "mealie_mock" ? "Mealieモック" : "Mealie")
      : recipe.video_url ? "動画URL" : recipe.source_url ? "Web URL" : "手入力";
    const calories = calorieEstimateLabel(recipe);
    const cookingTime = cookingTimeEstimateLabel(recipe);
    return `<article class="recipe-card">
      <div class="recipe-card-image">${media}<span>${servingLabel} · ${recipe.steps.length}工程</span></div>
      <div class="recipe-card-body"><h3>${escapeHtml(recipe.title)}</h3><p>${escapeHtml(recipe.description || "自分のレシピノート")}</p>
      <div class="card-calories">${calories ? `推定 ${calories} kcal` : "推定カロリー 未算出"}</div>
      <div class="card-calories">${cookingTime ? `推定調理時間 ${cookingTime}` : "推定調理時間 未算出"}</div>
      <div class="tags"><span class="tag">${sourceLabel}</span>${recipe.tags.slice(0, 2).map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("")}</div>
      <button class="card-button" data-open="${recipe.id}">レシピを見る</button></div></article>`;
  }).join("");
}

function cookingTimeValue(minutes) {
  const value = Math.max(1, Math.ceil(Number(minutes)));
  if (!Number.isFinite(value)) return "";
  if (value < 60) return `${value}分`;
  const hours = Math.floor(value / 60);
  const remainder = value % 60;
  return `${hours}時間${remainder ? `${remainder}分` : ""}`;
}

function cookingTimeEstimateLabel(recipe) {
  const estimate = recipe?.cooking_time_estimate;
  if (estimate?.minimum_minutes == null) return "";
  const minimum = cookingTimeValue(estimate.minimum_minutes);
  const maximum = cookingTimeValue(estimate.maximum_minutes);
  if (!minimum) return "";
  return !maximum || minimum === maximum ? minimum : `${minimum}〜${maximum}`;
}

function cookingTimeEstimatePanel(recipe) {
  const estimate = recipe?.cooking_time_estimate;
  const label = cookingTimeEstimateLabel(recipe);
  if (!label) {
    return '<div class="time-estimate"><strong>推定調理時間：未算出</strong><small>工程が登録されていません。</small></div>';
  }
  const notes = [];
  if (estimate.explicit_step_count) notes.push(`時間記載 ${estimate.explicit_step_count}工程`);
  if (estimate.estimated_step_count) notes.push(`時間未記載 ${estimate.estimated_step_count}工程は作業内容から概算`);
  notes.push("参考値");
  return `<div class="time-estimate"><strong>推定調理時間：${label}</strong><small>${notes.join(" · ")}</small></div>`;
}

function calorieEstimateLabel(recipe, multiplier = 1) {
  const estimate = recipe?.calorie_estimate;
  const minimum = Number(estimate?.minimum_kcal);
  const maximum = Number(estimate?.maximum_kcal);
  if (!Number.isFinite(minimum) || estimate?.minimum_kcal == null) return "";
  const scaledMinimum = Math.max(0, Math.round(minimum * multiplier));
  const scaledMaximum = Number.isFinite(maximum)
    ? Math.max(scaledMinimum, Math.round(maximum * multiplier))
    : scaledMinimum;
  return scaledMinimum === scaledMaximum ? `${scaledMinimum}` : `${scaledMinimum}〜${scaledMaximum}`;
}

function calorieEstimatePanel(recipe, multiplier) {
  const estimate = recipe?.calorie_estimate;
  const label = calorieEstimateLabel(recipe, multiplier);
  if (!label) {
    return '<div class="calorie-estimate"><strong>推定カロリー：未算出</strong><small>数量・単位を確認できる材料がありません。</small></div>';
  }
  const notes = [`${estimate.estimated_ingredient_count}/${estimate.ingredient_count}材料から概算`];
  if (estimate.has_unestimated_ingredients) notes.push("算出対象外の材料あり");
  if (estimate.used_unit_assumptions) notes.push("個・本などは一般的な大きさで換算");
  return `<div class="calorie-estimate"><strong>${formatNumber(multiplier)}倍の推定：${label} kcal</strong><small>${notes.join(" · ")}</small></div>`;
}

function openDetail(recipe) {
  setMode("assist", false);
  state.selected = recipe;
  state.multiplier = 1;
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
  state.activeMode = ["assist", "shopping", "import"].includes(mode) ? mode : "assist";
  $("#assistView").classList.toggle("hidden", state.activeMode !== "assist");
  $("#shoppingView").classList.toggle("hidden", state.activeMode !== "shopping");
  $("#importView").classList.toggle("hidden", state.activeMode !== "import");
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
  const guidance = portionGuidance(recipe);
  if (!guidance.length) state.quantityMode = "recipe";
  const showingGuidance = state.quantityMode === "guidance";
  const syncButton = !recipe.read_only && state.runtime?.mealie_sync_enabled
    ? recipe.servings_confirmed === false
      ? '<button class="secondary-button" disabled>基準人数の確認後にMealie同期</button>'
      : recipe.mealie_sync_status === "synced"
      ? '<button class="secondary-button" disabled>Mealie同期済み</button>'
      : recipe.mealie_sync_status === "outcome_unknown"
        ? '<button class="secondary-button" data-action="verify-mealie">Mealieで確認</button>'
        : '<button class="secondary-button" data-action="sync-mealie">Mealieへ同期</button>'
    : "";
  const deleteControl = recipe.read_only
    ? '<p class="delete-guidance">このレシピを削除する場合はMealie側で削除し、一覧の「更新」を押してください。</p>'
    : '<button class="danger-button" data-action="delete-current">ローカル下書きを削除</button>';
  const normalizeButton = recipe.read_only && recipe.source_kind === "mealie" && state.runtime?.mealie_sync_enabled
    ? '<button class="secondary-button" data-action="normalize-current">分量を正規化</button>'
    : "";
  const reimportButton = recipe.read_only && recipe.source_kind === "mealie"
    && (videoEmbedUrl(recipe.video_url) || videoEmbedUrl(recipe.source_url))
    ? '<button class="secondary-button" data-action="reimport-current">動画から再取り込み</button>'
    : "";
  const shoppingKey = `${recipe.id}:shopping`;
  const taskKey = `${recipe.id}:tasks`;
  const shoppingBlocked = state.listOutcomeUnknown.has(shoppingKey);
  const taskBlocked = state.listOutcomeUnknown.has(taskKey);
  const shoppingButton = `<button class="secondary-button" data-action="add-shopping-list" ${
    recipe.ingredients?.length && state.runtime?.shopping_list_enabled && !shoppingBlocked ? "" : "disabled"
  }>${shoppingBlocked ? "買い物リストをHA側で確認" : "買い物リストへ追加"}</button>`;
  const taskButton = `<button class="secondary-button" data-action="add-task-list" ${
    recipe.steps?.length && state.runtime?.task_list_enabled && !taskBlocked ? "" : "disabled"
  }>${taskBlocked ? "タスクリストをHA側で確認" : "タスクリストへ追加"}</button>`;
  $("#detailView").innerHTML = `
    <button class="back-button" data-action="home">← レシピ一覧</button>
    <div class="detail-hero">
      <div class="detail-image">${image ? `<img src="${escapeHtml(image)}" alt="${escapeHtml(recipe.title)}">` : ""}</div>
      <div class="detail-copy"><p class="eyebrow">READY TO COOK</p><h1>${escapeHtml(recipe.title)}</h1><p>${escapeHtml(recipe.description)}</p>
        ${recipe.servings_confirmed === false ? '<p class="unresolved">基準人数は未確認です。編集画面で人数を確認するとMealieへ同期できます。</p>' : ""}
        ${calorieEstimatePanel(recipe, state.multiplier)}
        ${cookingTimeEstimatePanel(recipe)}
        <div class="serving-control"><button data-multiplier="-1" aria-label="倍率を下げる">−</button><strong>${formatNumber(state.multiplier)}倍</strong><button data-multiplier="1" aria-label="倍率を上げる">＋</button></div>
        <div class="quantity-mode" aria-label="分量の表示方法">
          <button class="${showingGuidance ? "" : "active"}" data-quantity-mode="recipe" aria-pressed="${!showingGuidance}">メニューの分量</button>
          <button class="${showingGuidance ? "active" : ""}" data-quantity-mode="guidance" aria-pressed="${showingGuidance}" ${guidance.length ? "" : "disabled"}>1人分の適量</button>
        </div>
        <div class="detail-actions"><button class="primary-button" data-action="start-cooking">調理をはじめる</button>${shoppingButton}${taskButton}<button class="secondary-button" data-action="edit-current">${recipe.read_only ? "下書きにコピー" : "編集する"}</button>${reimportButton}${normalizeButton}${syncButton}${deleteControl}</div>
      </div>
    </div>
    ${embeddedVideo ? `<section class="video-panel"><div class="video-frame"><iframe src="${escapeHtml(embeddedVideo)}" title="${escapeHtml(recipe.title)}の料理動画" loading="lazy" referrerpolicy="strict-origin-when-cross-origin" sandbox="allow-scripts allow-same-origin allow-presentation" allow="encrypted-media; picture-in-picture; fullscreen" allowfullscreen></iframe></div><a class="source-link" href="${escapeHtml(video)}" target="_blank" rel="noopener noreferrer">動画を別画面で開く</a></section>` : ""}
    <div class="recipe-columns"><section><p class="eyebrow">${showingGuidance ? "PORTION GUIDE" : "INGREDIENTS"}</p><h2>${showingGuidance ? "1人分の適量から計算" : "メニュー（レシピ）の分量"}</h2>
      ${showingGuidance ? '<p class="quantity-note">一般的な適量の目安です。レシピに記載された分量とは別に表示しています。</p>' : ""}
      <ul class="ingredient-list">
      ${(showingGuidance ? guidance : recipe.ingredients.map((item) => ({ ...item, amount_label: scaledIngredient(item, state.multiplier) }))).map((item) => `<li><span>${escapeHtml(item.name)}${item.note ? `<small> ${escapeHtml(item.note)}</small>` : ""}</span><strong>${escapeHtml(item.amount_label || item.amount)}</strong></li>`).join("")}
      </ul>${source ? `<a class="source-link" href="${escapeHtml(source)}" target="_blank" rel="noopener noreferrer">出典ページを開く</a>` : ""}${video && !embeddedVideo ? `<br><a class="source-link" href="${escapeHtml(video)}" target="_blank" rel="noopener noreferrer">動画を開く</a>` : ""}</section>
      <section><p class="eyebrow">DIRECTIONS</p><h2>作り方</h2><ol class="steps-list">${recipe.steps.map((step) => `<li>${escapeHtml(step)}</li>`).join("")}</ol></section></div>`;
}

async function addSelectedRecipeToList(kind, button) {
  const recipe = state.selected;
  if (!recipe) return;
  const label = kind === "shopping" ? "買い物リスト" : "タスクリスト";
  const key = `${recipe.id}:${kind}`;
  if (state.listOutcomeUnknown.has(key)) {
    showToast(`前回の${label}追加結果をHome Assistant側で確認してください`);
    return;
  }
  button.disabled = true;
  button.textContent = `${label}へ追加中…`;
  try {
    const result = await api("/api/lists/add", {
      method: "POST",
      body: JSON.stringify({
        request_id: crypto.randomUUID(),
        recipe_id: recipe.id,
        kind,
        multiplier: state.multiplier,
      }),
    });
    showToast(`${label}へ${result.added_count}件追加しました${result.simulated ? "（Stage）" : ""}`);
    renderDetail();
  } catch (error) {
    if (error.data?.status === "outcome_unknown") state.listOutcomeUnknown.add(key);
    renderDetail();
    showToast(error.message);
  }
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

async function normalizeCurrentRecipe(button) {
  const recipe = state.selected;
  if (!recipe || !recipe.read_only || recipe.source_kind !== "mealie") return;
  button.disabled = true;
  try {
    const draft = await api(`/api/catalog/recipes/${encodeURIComponent(recipe.id)}/normalize`, {
      method: "POST",
    });
    await loadRecipes($("#searchInput").value);
    const normalizedDraft = state.recipes.find((item) => item.id === draft.id) || draft;
    openDetail(normalizedDraft);
    showToast("分量を正規化しました。倍率変更または編集後にMealieへ同期できます");
  } catch (error) {
    button.disabled = false;
    showToast(error.message);
  }
}

function renderYoutubeReimportPreview(preview) {
  state.youtubeReimport = preview;
  const element = $("#youtubeReimportReview");
  const entries = Array.isArray(preview.entries) ? preview.entries : [];
  const creatorCounts = entries.reduce((counts, entry) => {
    const label = entry.creator || "動画URL";
    counts[label] = (counts[label] || 0) + 1;
    return counts;
  }, {});
  const creatorSummary = ["ジョージ", "炊事場", "ファビオ", "おかずJAPAN", "動画URL"]
    .filter((label) => creatorCounts[label])
    .map((label) => `${label} ${creatorCounts[label]}件`)
    .join("・");
  const summary = state.youtubeReimportSummary;
  const bulkStatus = summary
    ? `<p class="${summary.failed ? "unresolved" : ""}">全件処理：${summary.processed}/${summary.total}件・確認待ち ${summary.ready}件・要確認あり ${summary.warning}件・失敗 ${summary.failed}件</p><p>確認待ち結果はこの画面を閉じる前に1件ずつ確認・保存してください。</p>`
    : "";
  element.innerHTML = `<strong>再取り込み対象 ${entries.length}件</strong>
    <p>Mealie ${Number(preview.mealie_count || 0)}件・ローカル下書き ${Number(preview.draft_count || 0)}件</p>
    ${creatorSummary ? `<p>${escapeHtml(creatorSummary)}</p>` : ""}
    <p>各レシピを再取得するとローカル下書きが開きます。内容を確認・保存した後に、Mealieへ同期してください。</p>
    <button class="primary-button" type="button" data-action="bulk-reimport-youtube" ${entries.length && !state.youtubeReimportRunning ? "" : "disabled"}>${state.youtubeReimportRunning && summary ? `${summary.processed}/${summary.total}件を再取り込み中…` : "全件再取り込み"}</button>
    ${bulkStatus}
    <ul>${entries.map((entry) => {
      const source = safeUrl(entry.source_url);
      const location = entry.source_kind === "draft" ? "ローカル下書き" : "Mealie";
      const buttonLabel = entry.bulk_status === "ready"
        ? "再取り込み結果を確認"
        : entry.bulk_status === "failed"
          ? "再試行"
          : entry.bulk_status === "saved"
            ? "保存済みを確認"
            : entry.draft_status === "review" ? "下書きを確認" : "再取り込み";
      return `<li><strong>${escapeHtml(entry.creator || "動画URL")}</strong> — ${escapeHtml(entry.title)} <small>（${location}）</small>
      ${source ? `<a class="source-link" href="${escapeHtml(source)}" target="_blank" rel="noopener noreferrer">動画を確認</a>` : ""}
      ${entry.bulk_error ? `<div class="unresolved">${escapeHtml(entry.bulk_error)}</div>` : ""}
      <button class="secondary-button" type="button" data-reimport-id="${escapeHtml(entry.id)}">${buttonLabel}</button>
    </li>`;
    }).join("")}</ul>`;
  element.classList.remove("hidden");
}

async function previewYoutubeReimport(button) {
  button.disabled = true;
  button.textContent = "対象を確認中…";
  try {
    const preview = await api("/api/reimport/youtube/preview");
    state.youtubeReimportDrafts.clear();
    state.youtubeReimportSummary = null;
    renderYoutubeReimportPreview(preview);
    showToast(`YouTube由来の既存レシピは${preview.count}件です`);
  } catch (error) {
    showToast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "対象レシピを再確認";
  }
}

async function reimportYoutubeRecipe(recipeId, button) {
  if (!recipeId) return;
  const prepared = state.youtubeReimportDrafts.get(recipeId);
  if (prepared) {
    openEditor(prepared, true);
    showToast("再取り込み結果を開きました。内容を確認して保存してください");
    return;
  }
  button.disabled = true;
  button.textContent = "再取得中…";
  try {
    const draft = await api(`/api/catalog/recipes/${encodeURIComponent(recipeId)}/reimport-video`, {
      method: "POST",
    });
    await loadRecipes();
    openEditor(draft, true);
    showToast(draft.reimport_status === "existing_draft"
      ? "確認中の下書きを開きました"
      : draft.import_warnings?.length
        ? "再取り込みしました。要確認項目を確認してください"
        : "再取り込みしました。内容を確認してください");
    if (state.youtubeReimport) {
      const entry = state.youtubeReimport.entries.find((item) => item.id === recipeId);
      if (entry) entry.draft_status = draft.reimport_status === "preview" ? "local" : "review";
      renderYoutubeReimportPreview(state.youtubeReimport);
    }
  } catch (error) {
    button.disabled = false;
    button.textContent = "再取り込み";
    showToast(error.message);
  }
}

async function bulkReimportYoutubeRecipes() {
  const entries = state.youtubeReimport?.entries || [];
  if (!entries.length || state.youtubeReimportRunning) return;
  if (!window.confirm(
    `対象${entries.length}件を20件ずつ再取り込みします。結果は確認待ちになり、Mealieへは同期しません。続けますか？`,
  )) return;

  state.youtubeReimportRunning = true;
  state.youtubeReimportDrafts.clear();
  state.youtubeReimportSummary = {
    total: entries.length,
    processed: 0,
    ready: 0,
    warning: 0,
    failed: 0,
  };
  entries.forEach((entry) => {
    delete entry.bulk_status;
    delete entry.bulk_error;
  });
  renderYoutubeReimportPreview(state.youtubeReimport);

  for (let batchStart = 0; batchStart < entries.length; batchStart += 20) {
    const batch = entries.slice(batchStart, batchStart + 20);
    for (const entry of batch) {
      try {
        const draft = await api(`/api/catalog/recipes/${encodeURIComponent(entry.id)}/reimport-video`, {
          method: "POST",
        });
        state.youtubeReimportDrafts.set(entry.id, draft);
        entry.bulk_status = "ready";
        state.youtubeReimportSummary.ready += 1;
        if (draft.import_warnings?.length) state.youtubeReimportSummary.warning += 1;
      } catch (error) {
        entry.bulk_status = "failed";
        entry.bulk_error = error.message;
        state.youtubeReimportSummary.failed += 1;
      }
      state.youtubeReimportSummary.processed += 1;
      renderYoutubeReimportPreview(state.youtubeReimport);
    }
    await new Promise((resolve) => setTimeout(resolve, 0));
  }

  state.youtubeReimportRunning = false;
  renderYoutubeReimportPreview(state.youtubeReimport);
  await loadRecipes();
  showToast(
    `全件再取り込み完了：確認待ち${state.youtubeReimportSummary.ready}件・失敗${state.youtubeReimportSummary.failed}件`,
  );
}

function reimportCurrentRecipe(button) {
  if (!state.selected?.id) return;
  reimportYoutubeRecipe(state.selected.id, button);
}

async function verifyMealieSync(button) {
  const recipe = state.selected;
  if (!recipe || recipe.read_only || recipe.mealie_sync_status !== "outcome_unknown") return;
  button.disabled = true;
  try {
    const result = await api("/api/mealie/sync/verify", {
      method: "POST",
      body: JSON.stringify({ recipe_id: recipe.id }),
    });
    await loadRecipes($("#searchInput").value);
    const next = result.status === "synced"
      ? state.recipes.find((item) => item.id === result.mealie_slug)
      : state.recipes.find((item) => item.id === recipe.id);
    if (next) openDetail(next);
    showToast(result.status === "synced"
      ? "Mealieへの反映を確認しました"
      : "Mealieには未反映でした。内容を確認して再同期できます");
  } catch (error) {
    button.disabled = false;
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
    const amountText = parts[0];
    const range = amountText.split(/[〜～~-]/).map((value) => value.trim());
    const parseAmount = (value) => {
      const fraction = value.match(/^(\d+)\s*\/\s*(\d+)$/);
      return fraction && Number(fraction[2]) !== 0
        ? Number(fraction[1]) / Number(fraction[2])
        : Number(value);
    };
    const amount = parseAmount(range[0]);
    const amountMax = range.length === 2 ? parseAmount(range[1]) : null;
    return {
      amount: Number.isFinite(amount) ? amount : null,
      amount_max: Number.isFinite(amountMax) && amountMax >= amount ? amountMax : null,
      amount_text: amountText,
      unit: parts[1],
      name: parts.slice(2).join(" | "),
      note: "",
      original_text: line,
    };
  }
  return { amount: null, amount_max: null, amount_text: "", unit: "", name: line.trim(), note: "", original_text: line };
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
  $("#servingsConfirmedField").checked = recipe?.servings_confirmed !== false;
  $("#portionKindField").value = recipe?.portion_kind || "standard";
  $("#meatPerPersonField").value = recipe?.meat_g_per_person ?? 200;
  $("#servingVolumeField").value = recipe?.serving_volume_ml ?? 300;
  $("#concentratePartsField").value = recipe?.dilution_concentrate_parts ?? 3;
  $("#mixerPartsField").value = recipe?.dilution_mixer_parts ?? 7;
  $("#ricePerPersonField").value = recipe?.rice_go_per_person ?? 0;
  $("#tagsField").value = recipe?.tags?.join(", ") || "";
  $("#descriptionField").value = recipe?.description || "";
  $("#ingredientsField").value = recipe?.ingredients?.map((item) => item.amount == null
    ? (item.original_text || `${item.name}${item.note ? `（${item.note}）` : ""}`)
    : `${item.amount_text || formatNumber(item.amount)} | ${item.unit} | ${item.name}`
  ).join("\n") || "";
  $("#stepsField").value = recipe?.steps?.join("\n") || "";
  $("#stepTimestampsField").value = recipe?.steps?.map((_, index) => (
    formatVideoTimestamp(recipe?.step_video_timestamps?.[index])
  )).join("\n") || "";
  $("#sourceField").value = recipe?.source_url || "";
  $("#videoField").value = recipe?.video_url || "";
  const importWarnings = Array.isArray(recipe?.import_warnings) ? recipe.import_warnings : [];
  const comparison = recipe?.reimport_comparison;
  const comparisonMessage = comparison
    ? `再取り込み差分：材料 ${comparison.previous_ingredient_count}→${comparison.new_ingredient_count}件、工程 ${comparison.previous_step_count}→${comparison.new_step_count}件、基準人数 ${comparison.previous_servings ?? "未確認"}→${comparison.new_servings ?? "未確認"}`
    : "";
  $("#formMessage").textContent = [
    importWarnings.length ? `要確認：${importWarnings.join("／")}` : "",
    comparisonMessage,
  ].filter(Boolean).join("\n");
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
    servings_confirmed: $("#servingsConfirmedField").checked,
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
    for (const [candidateId, draft] of state.youtubeReimportDrafts.entries()) {
      if (draft.id !== recipe.id) continue;
      state.youtubeReimportDrafts.delete(candidateId);
      const entry = state.youtubeReimport?.entries?.find((item) => item.id === candidateId);
      if (entry) entry.bulk_status = "saved";
    }
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
    showToast(draft.jev_review?.approved
      ? "動画ページのレシピ情報を自動入力し、Jev検品を通過しました"
      : draft.import_warnings?.length
        ? "取得できなかった項目があります。要確認の内容を確認してください"
        : "動画ページのレシピ情報を自動入力しました");
  } catch (error) { showToast(error.message); }
  finally { button.disabled = false; button.textContent = "動画から自動入力"; }
}

function renderBundleReview(review) {
  const element = $("#bundleReview");
  const counts = review.counts || {};
  const batchLabel = Number(review.batch_count || 1) > 1 ? `・${Number(review.batch_count)}バッチ` : "";
  element.innerHTML = `<strong>保存候補 ${Number(counts.candidate || 0)}件・重複 ${Number(counts.duplicate || 0)}件・未確認項目あり ${Number(counts.unresolved || 0)}件${batchLabel}</strong>
    <ul>${review.entries.map((entry) => {
      const duplicate = entry.disposition === "duplicate";
      const unresolved = entry.unresolved_fields?.length
        ? `<div class="unresolved">未確認：${entry.unresolved_fields.map(escapeHtml).join("／")}</div>`
        : "";
      const yieldLabel = entry.servings_confirmed === false
        ? `基準人数未確認（仮値${entry.servings}）`
        : `${entry.servings}人分`;
      return `<li class="${duplicate ? "duplicate" : ""}">${escapeHtml(entry.title)} — ${duplicate ? `重複（${escapeHtml(entry.duplicate_title)}）` : `${yieldLabel}・材料${entry.ingredient_count}件・${entry.step_count}工程`}${unresolved}</li>`;
    }).join("")}</ul>`;
  element.classList.remove("hidden");
  const saveButton = $("#saveBundleButton");
  saveButton.textContent = `確認した${Number(counts.candidate || 0)}件を下書き保存`;
  saveButton.classList.toggle("hidden", !counts.candidate);
}

async function previewBundleImport(event) {
  event.preventDefault();
  const file = $("#bundleImportFile").files[0];
  if (!file) return;
  if (file.size > 5_000_000) {
    showToast("JSONファイルは5MB以下にしてください");
    return;
  }
  const button = event.currentTarget.querySelector("button");
  button.disabled = true;
  button.textContent = "確認中…";
  try {
    const sourcePayload = JSON.parse(await file.text());
    if (!sourcePayload || !Array.isArray(sourcePayload.recipes) || !sourcePayload.recipes.length) {
      throw new Error("recipesが必要です");
    }
    if (sourcePayload.recipes.length > 400) {
      throw new Error("一度に確認できるレシピは400件までです");
    }
    const batches = [];
    for (let offset = 0; offset < sourcePayload.recipes.length; offset += 20) {
      const payload = { ...sourcePayload, recipes: sourcePayload.recipes.slice(offset, offset + 20) };
      button.textContent = `確認中… ${batches.length + 1}/${Math.ceil(sourcePayload.recipes.length / 20)}`;
      const review = await api("/api/import/bundle/preview", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      batches.push({ payload, review });
    }
    const review = {
      entries: batches.flatMap((batch) => batch.review.entries || []),
      counts: batches.reduce((total, batch) => {
        for (const key of ["total", "candidate", "duplicate", "unresolved"]) {
          total[key] += Number(batch.review.counts?.[key] || 0);
        }
        return total;
      }, { total: 0, candidate: 0, duplicate: 0, unresolved: 0 }),
      batch_count: batches.length,
    };
    state.bundleImport = { batches, review };
    renderBundleReview(review);
  } catch (error) {
    state.bundleImport = null;
    $("#bundleReview").classList.add("hidden");
    $("#saveBundleButton").classList.add("hidden");
    showToast(error instanceof SyntaxError ? "JSONファイルの形式が正しくありません" : error.message);
  } finally {
    button.disabled = false;
    button.textContent = "重複と内容を確認";
  }
}

async function saveBundleImport() {
  const current = state.bundleImport;
  if (!current) return;
  const count = Number(current.review.counts?.candidate || 0);
  if (!count || !window.confirm(`確認した${count}件をローカル下書きとして保存しますか？`)) return;
  const button = $("#saveBundleButton");
  button.disabled = true;
  button.textContent = "保存中…";
  try {
    let savedCount = 0;
    for (let index = 0; index < current.batches.length; index += 1) {
      const batch = current.batches[index];
      const batchCount = Number(batch.review.counts?.candidate || 0);
      if (!batchCount) continue;
      button.textContent = `保存中… ${index + 1}/${current.batches.length}`;
      const latestReview = await api("/api/import/bundle/preview", {
        method: "POST",
        body: JSON.stringify(batch.payload),
      });
      if (latestReview.review_token !== batch.review.review_token) {
        throw new Error("プレビュー結果が変わりました。ファイルをもう一度確認してください");
      }
      const result = await api("/api/import/bundle/save", {
        method: "POST",
        body: JSON.stringify({
          ...batch.payload,
          review_token: latestReview.review_token,
          confirm_count: batchCount,
        }),
      });
      savedCount += Number(result.saved_count || 0);
    }
    state.bundleImport = null;
    $("#bundleImportForm").reset();
    $("#bundleReview").classList.add("hidden");
    button.classList.add("hidden");
    await loadRecipes();
    setMode("assist");
    showToast(`${savedCount}件のローカル下書きを保存しました`);
  } catch (error) {
    state.bundleImport = null;
    $("#bundleReview").classList.add("hidden");
    button.classList.add("hidden");
    showToast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = `確認した${count}件を下書き保存`;
  }
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
  return recipe.ingredients.map((item) => ({
    name: item.name,
    note: item.note || "",
    amount: scaledIngredient(item, state.multiplier),
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
    state.cookTab = "step";
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
  $("#cookVideoTab").disabled = !cookVideoUrl;
  if (!cookVideoUrl && state.cookTab === "video") state.cookTab = "step";
  $("#cookVideo").classList.toggle("hidden", !cookVideoUrl);
  $("#cookVideoEmpty").classList.toggle("hidden", Boolean(cookVideoUrl));
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
    ? "次へ"
    : state.step === recipe.steps.length - 1 ? "できあがり" : "次へ";
  setCookTab(state.cookTab);
  sessionStorage.setItem(`step:${recipe.id}`, state.step);
}

function setCookTab(tab) {
  const videoAvailable = !$("#cookVideoTab").disabled;
  state.cookTab = tab === "video" && videoAvailable ? "video" : "step";
  const showingVideo = state.cookTab === "video";
  $("#cookStepPanel").classList.toggle("hidden", showingVideo);
  $("#cookVideoPanel").classList.toggle("hidden", !showingVideo);
  $$('[data-cook-tab]').forEach((button) => {
    const active = button.dataset.cookTab === state.cookTab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
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
  if (target.dataset.reimportId) reimportYoutubeRecipe(target.dataset.reimportId, target);
  if (target.dataset.query != null) { $("#searchInput").value = target.dataset.query; loadRecipes(target.dataset.query); }
  if (target.dataset.shoppingMood != null) {
    $("#shoppingMood").value = target.dataset.shoppingMood;
    recommendShoppingRecipes(target.dataset.shoppingMood).catch((error) => showToast(error.message));
  }
  if (target.dataset.shoppingSelect) {
    state.shoppingSelectedId = target.dataset.shoppingSelect;
    state.shoppingMultiplier = 1;
    renderShoppingSuggestions();
  }
  if (target.dataset.shoppingMultiplier) {
    state.shoppingMultiplier = adjustMultiplier(state.shoppingMultiplier, Number(target.dataset.shoppingMultiplier));
    renderShoppingList();
  }
  if (target.dataset.multiplier) {
    state.multiplier = adjustMultiplier(state.multiplier, Number(target.dataset.multiplier));
    renderDetail();
  }
  if (target.dataset.quantityMode) {
    state.quantityMode = target.dataset.quantityMode;
    renderDetail();
  }
  if (target.dataset.cookTab) setCookTab(target.dataset.cookTab);
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
    "normalize-current": () => normalizeCurrentRecipe(target),
    "reimport-current": () => reimportCurrentRecipe(target),
    "preview-youtube-reimport": () => previewYoutubeReimport(target),
    "bulk-reimport-youtube": bulkReimportYoutubeRecipes,
    "verify-mealie": () => verifyMealieSync(target),
    "sync-mealie": () => syncCurrentRecipe(target),
    "add-shopping-list": () => addSelectedRecipeToList("shopping", target),
    "add-task-list": () => addSelectedRecipeToList("tasks", target),
    "delete-current": deleteCurrentRecipe,
    "refresh-recipes": () => loadRecipes($("#searchInput").value).catch((error) => showToast(error.message)),
    "start-cooking": startCooking,
    "announce-shopping": announceShoppingList,
    "reset-shopping-pantry": resetShoppingPantry,
    "stop-cooking": () => { disableCookVoice(false); $("#cookMode").classList.add("hidden"); document.body.style.overflow = ""; window.speechSynthesis?.cancel(); },
    "timer-stop": () => stopTimer(),
  };
  actions[target.dataset.action]?.();
});

document.addEventListener("change", (event) => {
  const target = event.target.closest("[data-pantry-key]");
  if (!target) return;
  if (target.checked) state.pantryIngredients.add(target.dataset.pantryKey);
  else state.pantryIngredients.delete(target.dataset.pantryKey);
  savePantryIngredients();
  renderShoppingList();
});

$("#searchForm").addEventListener("submit", (event) => { event.preventDefault(); loadRecipes($("#searchInput").value); });
$("#searchInput").addEventListener("input", (event) => { clearTimeout(state.searchTimer); state.searchTimer = setTimeout(() => loadRecipes(event.target.value), 250); });
$("#voiceSearch").addEventListener("click", () => setupRecognition($("#voiceSearch"), (text) => { $("#searchInput").value = text; loadRecipes(text); }));
$("#shoppingForm").addEventListener("submit", (event) => {
  event.preventDefault();
  recommendShoppingRecipes($("#shoppingMood").value).catch((error) => showToast(error.message));
});
$("#shoppingVoice").addEventListener("click", () => setupRecognition($("#shoppingVoice"), (text) => {
  $("#shoppingMood").value = text;
  recommendShoppingRecipes(text).catch((error) => showToast(error.message));
}));
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
$("#bundleImportForm").addEventListener("submit", previewBundleImport);
$("#saveBundleButton").addEventListener("click", saveBundleImport);
$("#prevStep").addEventListener("click", () => changeStep(-1));
$("#nextStep").addEventListener("click", () => changeStep(1));
$("#speakStep").addEventListener("click", () => sendCookingAction("show", true).catch((error) => showToast(error.message)));

function registerWebMcpTools() {
  const context = document.modelContext;
  if (!context?.registerTool) return;
  const registration = (tool) => Promise.resolve(context.registerTool(tool)).catch(() => {});
  const reviewedRecipeSchema = {
    type: "object",
    properties: {
      title: { type: "string", minLength: 1, maxLength: 160 },
      description: { type: "string", maxLength: 2000 },
      servings: { type: "number", exclusiveMinimum: 0, maximum: 100 },
      servings_confirmed: { type: "boolean" },
      source_url: { type: "string" },
      video_url: { type: "string" },
      ingredients: {
        type: "array",
        maxItems: 200,
        description: "概要欄に記載された材料を省略せず格納します。出典にない材料や分量は推測しません。",
        items: { type: "object", additionalProperties: true },
      },
      ingredients_complete: {
        type: "boolean",
        description: "公開概要欄の材料セクションを最後まで照合した場合だけtrueにします。",
      },
      steps: {
        type: "array",
        maxItems: 100,
        description: "動画や出典を冒頭から完成まで確認した工程です。途中までの要約を完全な工程として扱いません。",
        items: { type: "string" },
      },
      steps_complete: {
        type: "boolean",
        description: "動画または出典を料理の完成まで確認し、工程が途中で切れていない場合だけtrueにします。",
      },
      step_video_timestamps: {
        type: "array",
        maxItems: 100,
        items: { type: ["integer", "string", "null"] },
      },
      tags: { type: "array", maxItems: 30, items: { type: "string" } },
      unresolved_fields: { type: "array", maxItems: 20, items: { type: "string" } },
    },
    required: ["title", "servings", "servings_confirmed", "ingredients", "steps"],
    additionalProperties: true,
  };
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
    name: "suggest_recipes_by_mood",
    title: "気分からレシピを提案",
    description: "食べたい気分をもとに登録済みレシピを順位付けします。レシピや在庫は変更しません。",
    inputSchema: {
      type: "object",
      properties: {
        mood: { type: "string", minLength: 1, maxLength: 160 },
        max_results: { type: "integer", minimum: 1, maximum: 5 },
      },
      required: ["mood"],
      additionalProperties: false,
    },
    annotations: { readOnlyHint: true, untrustedContentHint: true },
    async execute(input) {
      if (!input || typeof input.mood !== "string") throw new Error("moodが必要です");
      const result = await api("/api/catalog/recipes");
      const limit = Math.min(5, Math.max(1, Number(input.max_results) || 3));
      const suggestions = rankShoppingRecipes(result.recipes, input.mood).slice(0, limit);
      return {
        mood: input.mood,
        suggestions: suggestions.map(({ recipe, reasons }) => ({
          id: recipe.id,
          title: recipe.title,
          reasons,
          base_quantity: "1倍",
          ingredient_count: recipe.ingredients.length,
        })),
      };
    },
  });
  registration({
    name: "get_shopping_list",
    title: "買い足す材料を取得",
    description: "選択したレシピの倍率換算済み材料を返します。既定では端末で家にあるとチェックした材料を除外します。",
    inputSchema: {
      type: "object",
      properties: {
        recipe_id: { type: "string", minLength: 1, maxLength: 160 },
        multiplier: { type: "number", minimum: 0.5, maximum: 10 },
        exclude_pantry: { type: "boolean" },
      },
      required: ["recipe_id"],
      additionalProperties: false,
    },
    annotations: { readOnlyHint: true, untrustedContentHint: true },
    async execute(input) {
      if (!input || typeof input.recipe_id !== "string") throw new Error("recipe_idが必要です");
      const result = await api("/api/catalog/recipes");
      const recipe = result.recipes.find((item) => item.id === input.recipe_id);
      if (!recipe) throw new Error("レシピが見つかりません");
      const multiplier = Math.min(10, Math.max(0.5, Number(input.multiplier) || 1));
      const excludePantry = input.exclude_pantry !== false;
      const ingredients = recipe.ingredients
        .filter((item) => !excludePantry || !state.pantryIngredients.has(shoppingIngredientKey(item)))
        .map((item) => ({
          name: item.name,
          amount: scaledIngredient(item, multiplier),
          needs_confirmation: item.amount == null && !["適量", "少々", "多めの少々", "ひとつまみ"].includes(String(item.note || "").trim()),
        }));
      return { recipe_id: recipe.id, title: recipe.title, multiplier, ingredients };
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
  registration({
    name: "preview_reviewed_recipe_bundle",
    title: "レビュー済みレシピの重複確認",
    description: "Mealieとローカル下書きを読み取り、確認済みレシピを保存せずに重複判定します。材料は完全な公開概要欄と照合し、工程は動画または出典を料理の完成まで確認してください。確認できない内容は推測せずunresolved_fieldsへ記録し、保存前に必ずこのツールを実行してください。",
    inputSchema: {
      type: "object",
      properties: {
        recipes: { type: "array", minItems: 1, maxItems: 20, items: reviewedRecipeSchema },
      },
      required: ["recipes"],
      additionalProperties: false,
    },
    annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false, untrustedContentHint: true },
    async execute(input) {
      if (!input || !Array.isArray(input.recipes)) throw new Error("recipesが必要です");
      return api("/api/import/bundle/preview", {
        method: "POST",
        body: JSON.stringify({ recipes: input.recipes }),
      });
    },
  });
  registration({
    name: "save_reviewed_recipe_bundle",
    title: "レビュー済みレシピを下書き保存",
    description: "直前に重複確認したレシピだけをローカル下書きへ保存します。review_tokenとユーザーが承認した正確な件数が必要です。Mealieへは公開しません。",
    inputSchema: {
      type: "object",
      properties: {
        recipes: { type: "array", minItems: 1, maxItems: 20, items: reviewedRecipeSchema },
        review_token: { type: "string", pattern: "^[a-f0-9]{64}$" },
        confirm_count: { type: "integer", minimum: 1, maximum: 20 },
      },
      required: ["recipes", "review_token", "confirm_count"],
      additionalProperties: false,
    },
    annotations: { readOnlyHint: false, destructiveHint: false, openWorldHint: false, untrustedContentHint: true },
    async execute(input) {
      if (!input || !Array.isArray(input.recipes)) throw new Error("recipesが必要です");
      const result = await api("/api/import/bundle/save", {
        method: "POST",
        body: JSON.stringify(input),
      });
      await loadRecipes();
      return result;
    },
  });
}

registerWebMcpTools();
Promise.all([loadRuntime(), loadRecipes()]).catch((error) => {
  $("#recipeGrid").innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
});
