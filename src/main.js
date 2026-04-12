const { invoke } = window.__TAURI__.core;
let lastSaveErrorToastAt = 0;
let globalErrorHooksReady = false;

// ===== STATE =====
const state = {
  queue: [],
  currentIndex: -1,
  isPlaying: false,
  isLoading: false,
  volume: 0.8,
  isMuted: false,
  lastQuery: '',
  playlists: {},
  favorites: [],
  history: [],
  searchHistory: [],
  currentPlaylistId: 'local',
  session: { video: null, time: 0 },
  errorCount: 0,
  settings: {
    persistState: true,
    isShuffle: false,
    repeatMode: 0,
    isAutoQueue: false,
    autoMinQueue: 2,
    autoTheme: '',
    outputDevice: 'default',
    inputDevice: 'default',
    blacklist_words: '',
    blacklist_chars: '',
    maxDuration: 600,
    maxDurationEnabled: false,
    closeToTray: true,
    volMin: 0,
    volMax: 1,
    repairThreads: 4,
    autoSearchDelay: 1.5,
    autoMixSize: 10,
    keybinds: [],
    showVersion: true,
    theme: {
      accent: '#7c3aed',
      accentLight: '#9f67ff',
      bg: '#0a0a0f',
      surface: '#16161f',
      border: '#2d2d3d',
      textPrimary: '#f0f0f8',
      textSecondary: '#9090b8'
    }
  },
  autoCandidates: [],
  searchMode: 'youtube', // 'youtube' | 'youtube_music'
  searchReference: '',
  audioCtx: null,
  notificationAudioCtx: null,
  notificationSoundUrl: '',
  notificationSoundBuffer: null,
  gainNode: null,
  activeRepairs: {}, // { playlistId: { checkedCount, total, brokenIndices, workersFinished, ... } }
  versionInfo: null,
  isAutoQueueSearching: false,
  currentScreen: 'welcome',
  isHandlingPlaybackError: false,
  lastPlaybackErrorAt: 0,
  lastPlaybackErrorVideoId: ''
};

// ===== HELPERS =====
function removeDiacritics(str) {
  if (!str) return '';
  return str.normalize("NFD").replace(/[\u0300-\u036f]/g, "");
}

function normalizeRecoveredTitle(title) {
  if (title === null || title === undefined) return '';
  return String(title)
    .replace(/&quot;/g, '"')
    .replace(/&amp;/g, '&')
    .replace(/&#39;/g, "'")
    .replace(/\\r\\n|\\r|\\n/g, ' ')
    .replace(/\r\n|\r|\n/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function getBlacklistTerms(type = 'words') {
  const raw = type === 'words' ? state.settings.blacklist_words : state.settings.blacklist_chars;
  return (raw || '').toLowerCase().split(',').map(k => k.trim()).filter(k => k);
}

function isValidRecoveredTitle(title) {
  if (!title || typeof title !== 'string') return false;
  const t = normalizeRecoveredTitle(title);
  if (!t) return false;

  if (/^video\s+[a-zA-Z0-9_-]{11}$/.test(t)) return false;

  const lower = t.toLowerCase();
  const invalid = [
    'deleted video', 'private video', 'video unavailable',
    'unknown', '[deleted]', '[private]',
    'just a moment', 'cloudflare', 'enable cookies',
    'video not found', 'attention required',
    'checking your browser', 'sign in to confirm your age',
    'antes de ir a youtube'
  ];
  return !invalid.some(k => lower.includes(k));
}

function needsTitleRecovery(title) {
  return !isValidRecoveredTitle(title || '');
}

function createKeybindId() {
  return `kb_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
}

function getDefaultKeybinds() {
  return [
    { id: 'default-play-pause', shortcut: 'MediaPlayPause', action: 'play-pause', builtin: true },
    { id: 'default-next', shortcut: 'MediaTrackNext', action: 'next-song', builtin: true },
    { id: 'default-prev', shortcut: 'MediaTrackPrevious', action: 'previous-song', builtin: true },
    { id: 'default-volume-up', shortcut: 'VolumeUp', action: 'volume-up', builtin: true },
    { id: 'default-volume-down', shortcut: 'VolumeDown', action: 'volume-down', builtin: true }
  ];
}

function normalizeKeybindItem(item) {
  if (!item || typeof item !== 'object') return null;
  let shortcut = String(item.shortcut || '').trim();
  const action = String(item.action || '').trim();
  if (!shortcut || !action) return null;
  if (shortcut === '+' || shortcut === 'Plus') {
    shortcut = 'NumpadAdd';
  }
  if (shortcut === '-' || shortcut === 'Minus') {
    shortcut = 'NumpadSubtract';
  }
  if (shortcut === '*' || shortcut === 'Multiply') {
    shortcut = 'NumpadMultiply';
  }
  if (shortcut === '/' || shortcut === 'Divide') {
    shortcut = 'NumpadDivide';
  }
  const pressCount = Math.max(1, Math.min(2, Math.floor(Number(item.pressCount ?? item.press_count ?? 1) || 1)));
  if (isUnsafeGlobalShortcut(shortcut)) return null;
  return {
    id: String(item.id || createKeybindId()),
    shortcut,
    action,
    pressCount,
    builtin: !!item.builtin
  };
}

function normalizeKeybinds(items) {
  const normalized = [];
  for (const item of Array.isArray(items) ? items : []) {
    const normalizedItem = normalizeKeybindItem(item);
    if (!normalizedItem) continue;
    normalized.push(normalizedItem);
  }
  return normalized;
}

function getKeybindActionLabel(action) {
  const labels = {
    'next-song': 'Siguiente canción',
    'previous-song': 'Canción anterior',
    'play-pause': 'Pausar / reproducir',
    'favorite-add': 'Añadir a favoritos',
    'notification-sound': 'Reproducir sonido de notificación',
    'volume-up': 'Subir volumen',
    'volume-down': 'Bajar volumen'
  };
  return labels[action] || action;
}

function formatPressCountLabel(pressCount) {
  return `${Math.max(1, Math.floor(Number(pressCount) || 1))}x`;
}

function formatShortcutLabel(shortcut) {
  const normalized = String(shortcut || '').trim();
  if (normalized === 'NumpadAdd') return '+';
  if (normalized === 'NumpadSubtract' || normalized === 'Minus') return '-';
  if (normalized === 'NumpadMultiply') return '*';
  if (normalized === 'NumpadDivide') return '/';
  if (normalized === 'AudioVolumeUp') return 'VolumeUp';
  if (normalized === 'AudioVolumeDown') return 'VolumeDown';
  return normalized;
}

function normalizeShortcutFromEvent(event) {
  const key = String(event?.key || '').trim();
  if (!key) return '';

  if (key.startsWith('Media') || key.startsWith('Volume') || key.startsWith('AudioVolume')) {
    if (key === 'MediaPlay' || key === 'MediaPause' || key === 'MediaStop') return 'MediaPlayPause';
    if (key === 'AudioVolumeUp') return 'VolumeUp';
    if (key === 'AudioVolumeDown') return 'VolumeDown';
    return key;
  }

  if (['Control', 'Shift', 'Alt', 'Meta'].includes(key)) {
    return '';
  }

  const modifiers = [];
  if (event.ctrlKey) modifiers.push('Ctrl');
  if (event.altKey) modifiers.push('Alt');
  if (event.shiftKey) modifiers.push('Shift');
  if (event.metaKey) modifiers.push('Meta');

  let base = key;
  if (base === ' ') base = 'Space';
  else if (base === '+' || event?.code === 'NumpadAdd') base = 'NumpadAdd';
  else if (base === '-' || event?.code === 'NumpadSubtract') base = 'NumpadSubtract';
  else if (base === '*' || event?.code === 'NumpadMultiply') base = 'NumpadMultiply';
  else if (base === '/' || event?.code === 'NumpadDivide') base = 'NumpadDivide';
  else if (base.length === 1) base = base.toUpperCase();

  return [...modifiers, base].join('+');
}

function shortcutForBindableAction(action) {
  const mapping = {
    'next-song': 'MediaTrackNext',
    'previous-song': 'MediaTrackPrevious',
    'play-pause': 'MediaPlayPause',
    'notification-sound': 'MediaPlayPause',
    'volume-up': 'VolumeUp',
    'volume-down': 'VolumeDown'
  };

  return mapping[String(action || '')] || '';
}

const MEDIA_SHORTCUT_DOUBLE_WINDOW_MS = 450;
const mediaShortcutPressState = new Map();
const mediaShortcutSingleTimers = new Map();

function normalizeShortcutKey(shortcut) {
  const lower = String(shortcut || '').trim().toLowerCase();
  switch (lower) {
    case 'mediaplaypause':
    case 'mediaplay':
    case 'mediapause':
    case 'mediastop':
      return 'mediaplaypause';
    case 'audiovolumeup':
    case 'volumeup':
    case 'volumeincrease':
      return 'volumeup';
    case 'audiovolumedown':
    case 'volumedown':
    case 'volumedecrease':
      return 'volumedown';
    case 'numpadadd':
    case 'add':
    case 'plus':
      return 'numpadadd';
    case 'numpadsubtract':
    case 'subtract':
    case 'minus':
      return 'numpadsubtract';
    case 'numpadmultiply':
    case 'multiply':
    case 'asterisk':
      return 'numpadmultiply';
    case 'numpaddivide':
    case 'divide':
      return 'numpaddivide';
    default:
      return lower;
  }
}

function getBindingsForShortcut(shortcut) {
  const key = normalizeShortcutKey(shortcut);
  return normalizeKeybinds(state.settings.keybinds)
    .filter(binding => normalizeShortcutKey(binding.shortcut) === key);
}

async function executeBoundActionsForShortcut(shortcut, fallbackAction = '') {
  const key = normalizeShortcutKey(shortcut);

  if (keybindCaptureState.open) {
    const canonical = shortcutForBindableAction(fallbackAction) || shortcut;
    if (canonical) {
      keybindCaptureState.capturedShortcut = canonical;
      setKeybindCaptureText(canonical);
      showToast(`Capturado: ${canonical}`);
      return true;
    }
  }

  const bindings = getBindingsForShortcut(shortcut);

  if (bindings.length === 0) {
    if (fallbackAction) {
      await handleKeybindAction(fallbackAction);
      return true;
    }
    return false;
  }

  const now = Date.now();
  const previous = mediaShortcutPressState.get(key);
  const pressCount = previous && (now - previous.ts) <= MEDIA_SHORTCUT_DOUBLE_WINDOW_MS
    ? previous.count + 1
    : 1;

  mediaShortcutPressState.set(key, { ts: now, count: pressCount });

  const runByCount = async (count) => {
    for (const binding of bindings) {
      const targetCount = Math.max(1, Math.min(2, Number(binding.pressCount || 1)));
      if (targetCount === count) {
        await handleKeybindAction(binding.action);
      }
    }
  };

  if (pressCount === 1) {
    const hasDouble = bindings.some(binding => Math.max(1, Math.min(2, Number(binding.pressCount || 1))) === 2);
    if (!hasDouble) {
      await runByCount(1);
      mediaShortcutPressState.delete(key);
      return true;
    }

    const existingTimer = mediaShortcutSingleTimers.get(key);
    if (existingTimer) {
      clearTimeout(existingTimer);
    }

    const timer = setTimeout(() => {
      const current = mediaShortcutPressState.get(key);
      if (!current) return;
      if (current.ts === now && current.count === 1) {
        void runByCount(1);
        mediaShortcutPressState.delete(key);
      }
      mediaShortcutSingleTimers.delete(key);
    }, MEDIA_SHORTCUT_DOUBLE_WINDOW_MS);

    mediaShortcutSingleTimers.set(key, timer);
    return true;
  }

  if (pressCount === 2) {
    const pending = mediaShortcutSingleTimers.get(key);
    if (pending) {
      clearTimeout(pending);
      mediaShortcutSingleTimers.delete(key);
    }
    await runByCount(2);
    mediaShortcutPressState.delete(key);
    return true;
  }

  mediaShortcutPressState.set(key, { ts: now, count: 1 });
  return true;
}

function isUnsafeGlobalShortcut(shortcut) {
  const normalized = String(shortcut || '').trim();
  if (!normalized) return true;

  if (normalized.includes('+')) return false;

  const key = normalizeShortcutKey(normalized);
  if (key.startsWith('f')) {
    const functionKey = Number(key.slice(1));
    if (Number.isInteger(functionKey) && functionKey >= 1 && functionKey <= 24) {
      return false;
    }
  }

  if (
    key === 'mediaplaypause' ||
    key === 'volumeup' ||
    key === 'volumedown' ||
    key === 'numpadadd' ||
    key === 'numpadsubtract' ||
    key === 'numpadmultiply' ||
    key === 'numpaddivide'
  ) {
    return false;
  }

  return true;
}

async function loadNotificationSoundAsset() {
  if (state.notificationSoundUrl !== '') return state.notificationSoundUrl;

  try {
    state.notificationSoundUrl = String(await invoke('get_notification_sound_data_url') || '').trim();
  } catch (e) {
    console.warn('No se pudo cargar el sonido embebido de notificación:', e);
    state.notificationSoundUrl = '';
  }

  return state.notificationSoundUrl;
}

async function ensureNotificationSoundBuffer() {
  if (state.notificationSoundBuffer) return state.notificationSoundBuffer;

  const soundUrl = await loadNotificationSoundAsset();
  if (!soundUrl) return null;

  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  if (!AudioContextClass) return null;

  if (!state.notificationAudioCtx) {
    state.notificationAudioCtx = new AudioContextClass();
  }

  try {
    const response = await fetch(soundUrl);
    const arrayBuffer = await response.arrayBuffer();
    state.notificationSoundBuffer = await state.notificationAudioCtx.decodeAudioData(arrayBuffer.slice(0));
    return state.notificationSoundBuffer;
  } catch (e) {
    console.warn('No se pudo decodificar el sonido embebido de notificación:', e);
    return null;
  }
}

function cloneFavoriteSong(song) {
  if (!song || typeof song !== 'object') return null;

  const id = getEntryVideoId(song) || String(song.id || '').trim();
  if (!id) return null;

  return {
    ...song,
    id
  };
}

async function addSongToFavorites(song = state.queue[state.currentIndex], { updateCurrentHeart = true, playSound = true } = {}) {
  const currentSong = cloneFavoriteSong(song);
  if (!currentSong) {
    showToast('No hay canción actual', true);
    return false;
  }

  const exists = state.favorites.some(f => f.id === currentSong.id);
  if (exists) {
    showToast('Ya está en favoritos');
    return false;
  }

  state.favorites.push(currentSong);
  if (updateCurrentHeart && heartBtn && state.queue[state.currentIndex]?.id === currentSong.id) {
    heartBtn.classList.add('active');
  }

  try {
    await saveData();
  } catch (e) {
    console.warn('No se pudo guardar el favorito:', e);
  }

  updatePlayButton();

  if (playSound) {
    void playNotificationSound();
  }

  showToast('Añadido a favoritos');
  return true;
}

async function playNotificationSound() {
  try {
    const buffer = await ensureNotificationSoundBuffer();
    if (!buffer || !state.notificationAudioCtx) return;

    const ctx = state.notificationAudioCtx;
    if (ctx.state === 'suspended') {
      await ctx.resume().catch(() => {});
    }

    if (ctx.state !== 'running') {
      return;
    }

    const source = ctx.createBufferSource();
    const gain = ctx.createGain();
    source.buffer = buffer;
    gain.gain.value = 1;
    source.connect(gain);
    gain.connect(ctx.destination);

    source.start();
  } catch (e) {
    console.warn('No se pudo reproducir el sonido de notificación:', e);
  }
}

async function handleKeybindAction(action) {
  if (keybindCaptureState.open) {
    if (!keybindCaptureState.capturedShortcut) {
      const capturedShortcut = shortcutForBindableAction(action);
      if (capturedShortcut) {
        keybindCaptureState.capturedShortcut = capturedShortcut;
        setKeybindCaptureText(capturedShortcut);
        showToast(`Capturado: ${capturedShortcut}`);
      }
    }
    return;
  }

  switch (String(action || '')) {
    case 'next-song':
      playNext();
      break;
    case 'previous-song':
      playPrev();
      break;
    case 'play-pause':
      togglePlay();
      break;
    case 'favorite-add':
      await addSongToFavorites(undefined, { playSound: true });
      break;
    case 'notification-sound':
      await playNotificationSound();
      break;
    case 'volume-up':
      setVolume(state.volume + 0.1);
      showToast(`Volumen: ${Math.round(state.volume * 100)}%`);
      break;
    case 'volume-down':
      setVolume(state.volume - 0.1);
      showToast(`Volumen: ${Math.round(state.volume * 100)}%`);
      break;
    default:
      console.warn('Accion de bindeo desconocida:', action);
  }
}

async function appendSessionLog(message) {
  const text = normalizeRecoveredTitle(String(message || ''));
  if (!text) return;

  try {
    await invoke('append_session_log', { message: text });
  } catch (e) {
    console.warn('No se pudo escribir en log.txt:', e);
  }
}

function installGlobalErrorLogging() {
  if (globalErrorHooksReady) return;
  globalErrorHooksReady = true;

  window.addEventListener('error', (event) => {
    const where = `${event.filename || 'unknown'}:${event.lineno || 0}:${event.colno || 0}`;
    const message = event?.error ? stringifyError(event.error) : String(event?.message || 'Error desconocido');
    appendSessionLog(`[JS][window.error] ${where} ${message}`);
  });

  window.addEventListener('unhandledrejection', (event) => {
    const message = stringifyError(event?.reason || 'Unhandled rejection');
    appendSessionLog(`[JS][unhandledrejection] ${message}`);
  });
}

async function handlePlaybackFailure(video, source, err) {
  const song = video || state.queue[state.currentIndex] || state.session.video;
  const videoId = song?.id || 'unknown';
  const title = normalizeRecoveredTitle(song?.title || '') || `Video ${videoId}`;
  const errorText = stringifyError(err) || 'Error desconocido';
  const now = Date.now();

  if (
    state.lastPlaybackErrorVideoId === videoId
    && now - state.lastPlaybackErrorAt < 1500
  ) {
    return;
  }

  if (state.isHandlingPlaybackError) {
    return;
  }

  state.isHandlingPlaybackError = true;
  try {
    state.lastPlaybackErrorAt = now;
    state.lastPlaybackErrorVideoId = videoId;
    state.errorCount = (Number(state.errorCount) || 0) + 1;

    await appendSessionLog(`[PLAYBACK][${source}] id=${videoId} title="${title}" error=${errorText}`);

    state.isPlaying = false;
    state.isLoading = false;
    updatePlayButton();
    equalizer.classList.remove('active');
    schedulePlaybackSessionSave(0);

    const hasQueue = state.queue.length > 0 && state.currentIndex >= 0;
    const canAdvance = hasQueue && (
      state.settings.repeatMode === 1
      || (state.settings.isShuffle && state.queue.length > 1)
      || state.currentIndex < state.queue.length - 1
    );

    if (canAdvance) {
      showToast(`Cancion caida: ${title}. Saltando...`, true);
      playNext();
    } else if (state.settings.isAutoQueue) {
      showToast(`Cancion caida: ${title}. Buscando reemplazo...`, true);
      try {
        await checkAutoQueue();
      } catch (autoErr) {
        await appendSessionLog(`[PLAYBACK][AUTOQUEUE_FAIL] id=${videoId} error=${stringifyError(autoErr)}`);
      }
    } else {
      showToast(`Cancion caida: ${title}`, true);
    }
  } finally {
    state.isHandlingPlaybackError = false;
  }
}

const recoveredTitleCache = new Map();

async function recoverTitleForVideoId(videoId, currentTitle = 'Unknown') {
  if (!videoId) return currentTitle || 'Unknown';

  if (recoveredTitleCache.has(videoId)) {
    return recoveredTitleCache.get(videoId);
  }

  const attempts = ['recover_from_techrobo', 'recover_from_filmot', 'recover_from_wayback'].map((cmd) => (
    invoke(cmd, { videoId })
      .then((recovered) => {
        const clean = normalizeRecoveredTitle(recovered);
        if (isValidRecoveredTitle(clean)) {
          return clean;
        }
        throw new Error(`${cmd}_invalid_title`);
      })
  ));

  try {
    const recovered = await Promise.any(attempts);
    recoveredTitleCache.set(videoId, recovered);
    return recovered;
  } catch (_) {
    // Keep falling back to the best title we already have.
  }

  const fallbackCandidate = normalizeRecoveredTitle(currentTitle);
  const fallback = isValidRecoveredTitle(fallbackCandidate) ? fallbackCandidate : 'Unknown';
  recoveredTitleCache.set(videoId, fallback);
  return fallback;
}

async function recoverTitlesBatch(entries, recoverCandidates, concurrency = 4) {
  if (!Array.isArray(recoverCandidates) || recoverCandidates.length === 0) return 0;

  const workerCount = Math.max(1, Math.min(10, Number(concurrency) || 4));
  let cursor = 0;
  let recovered = 0;

  const worker = async () => {
    while (cursor < recoverCandidates.length) {
      const taskIndex = cursor;
      cursor += 1;

      const { song, idx } = recoverCandidates[taskIndex];
      const title = await recoverTitleForVideoId(song.id, song.title);
      entries[idx] = { ...song, title: normalizeRecoveredTitle(title) || song.title || 'Unknown' };
      recovered += 1;
    }
  };

  await Promise.all(Array.from({ length: Math.min(workerCount, recoverCandidates.length) }, worker));
  return recovered;
}

function isBlacklisted(video) {
  if (!video) return false;

  const bWords = getBlacklistTerms('words');
  const bChars = getBlacklistTerms('chars');

  if (bWords.length === 0 && bChars.length === 0) return false;

  let title = (video.title || '').toLowerCase();
  let artist = (video.uploader || video.channel || '').toLowerCase();

  // Normalizar acentos
  title = removeDiacritics(title);
  artist = removeDiacritics(artist);

  // Check Chars (Coincidencia parcial/caracteres)
  const charMatch = bChars.some(term => {
    const cleanTerm = removeDiacritics(term.toLowerCase());
    return title.includes(cleanTerm) || artist.includes(cleanTerm);
  });
  if (charMatch) return true;

  // Check Words (Coincidencia de palabra completa)
  const wordMatch = bWords.some(term => {
    const cleanTerm = removeDiacritics(term.toLowerCase());
    // Escapar caracteres especiales para RegEx
    const escaped = cleanTerm.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const regex = new RegExp(`(^|\\s|[.,!?;:()"])${escaped}($|\\s|[.,!?;:()"])`, 'i');
    return regex.test(title) || regex.test(artist);
  });

  return wordMatch;
}


// ===== DOM REFS =====
const audio = document.getElementById('audioPlayer');
const searchInput = document.getElementById('searchInput');
const searchBtn = document.getElementById('searchBtn');
const welcomeScreen = document.getElementById('welcomeScreen');
const loadingScreen = document.getElementById('loadingScreen');
const loadingQuery = document.getElementById('loadingQuery');
const resultsGrid = document.getElementById('resultsGrid');
const resultsTitle = document.getElementById('resultsTitle');
const resultsCount = document.getElementById('resultsCount');
const resultsActions = document.getElementById('resultsActions');
const cardsGrid = document.getElementById('cardsGrid');
const errorScreen = document.getElementById('errorScreen');
const errorMessage = document.getElementById('errorMessage');
const retryBtn = document.getElementById('retryBtn');

const playlistSidebar = document.getElementById('playlistSidebar');
const playlistToggleBtn = document.getElementById('playlistToggleBtn');
const importedPlaylistsList = document.getElementById('importedPlaylistsList');
const localPlaylist = document.getElementById('localPlaylist');
const importPlaylistBtn = document.getElementById('importPlaylistBtn');
const searchSuggestions = document.getElementById('searchSuggestions');
const playlistImportArea = document.getElementById('playlistImportArea');
const playlistUrlInput = document.getElementById('playlistUrlInput');
const confirmImportBtn = document.getElementById('confirmImportBtn');

const queueSidebar = document.getElementById('queueSidebar');
const queueToggleBtn = document.getElementById('queueToggleBtn');
const queueList = document.getElementById('queueList');
const clearQueueBtn = document.getElementById('clearQueueBtn');
const sidebarShuffleBtn = document.getElementById('sidebarShuffleBtn');
const sidebarRepeatBtn = document.getElementById('sidebarRepeatBtn');
const sidebarAutoBtn = document.getElementById('sidebarAutoBtn');

const playBtn = document.getElementById('playBtn');
const prevBtn = document.getElementById('prevBtn');
const nextBtn = document.getElementById('nextBtn');
const autoBtn = document.getElementById('autoBtn');
const shuffleBtn = document.getElementById('shuffleBtn');
const repeatBtn = document.getElementById('repeatBtn');
const heartBtn = document.getElementById('heartBtn');
const playSpinner = document.getElementById('playSpinner');
const iconPlay = playBtn.querySelector('.icon-play');
const iconPause = playBtn.querySelector('.icon-pause');
const repeatBadges = document.querySelectorAll('.repeat-one-badge');

const nowPlayingTitle = document.getElementById('nowPlayingTitle');
const nowPlayingArtist = document.getElementById('nowPlayingArtist');
const nowPlayingImg = document.getElementById('nowPlayingImg');
const equalizer = document.getElementById('equalizer');

const modeIndicator = document.getElementById('modeIndicator');
const modeIconYt = document.querySelector('.mode-icon-yt');
const modeIconYtm = document.querySelector('.mode-icon-ytm');

const searchReferenceEl = document.querySelector('.search-reference');

const progressFill = document.getElementById('progressFill');
const progressThumb = document.getElementById('progressThumb');
const progressBar = document.getElementById('progressBar');
const currentTimeEl = document.getElementById('currentTime');
const totalTimeEl = document.getElementById('totalTime');

const muteBtn = document.getElementById('muteBtn');
const volumeBar = document.getElementById('volumeBar');
const volumeFill = document.getElementById('volumeFill');
const volumeThumb = document.getElementById('volumeThumb');
const ytdlpStatus = document.getElementById('ytdlpStatus');
const statusText = ytdlpStatus.querySelector('.status-text');

// Settings DOM
const showSettingsBtn = document.getElementById('showSettingsBtn');
const showCustomBtn = document.getElementById('showCustomBtn');
const showBindsBtn = document.getElementById('showBindsBtn');
const settingsScreen = document.getElementById('settingsScreen');
const customizationScreen = document.getElementById('customizationScreen');
const bindsScreen = document.getElementById('bindsScreen');

const settingPersistState = document.getElementById('settingPersistState');
const settingCloseToTray = document.getElementById('settingCloseToTray');
const settingOutputDevice = document.getElementById('settingOutputDevice');
const settingInputDevice = document.getElementById('settingInputDevice');
const settingBlacklistWords = document.getElementById('settingBlacklistWords');
const settingBlacklistChars = document.getElementById('settingBlacklistChars');
const settingMaxDuration = document.getElementById('settingMaxDuration');
const settingMaxDurationEnabled = document.getElementById('settingMaxDurationEnabled');
const settingVolMin = document.getElementById('settingVolMin');
const settingVolMax = document.getElementById('settingVolMax');
const settingRepairThreads = document.getElementById('settingRepairThreads');
const settingAutoSearchDelay = document.getElementById('settingAutoSearchDelay');
const settingAutoMixSize = document.getElementById('settingAutoMixSize');
const valRepairThreads = document.getElementById('valRepairThreads');
const valAutoSearchDelay = document.getElementById('valAutoSearchDelay');
const valAutoMixSize = document.getElementById('valAutoMixSize');
const settingShowVersion = document.getElementById('settingShowVersion');
const saveSettingsBtn = document.getElementById('saveSettingsBtn');
const addKeybindBtn = document.getElementById('addKeybindBtn');
const keybindsList = document.getElementById('keybindsList');
const keybindCaptureModal = document.getElementById('keybindCaptureModal');
const keybindModalTitle = document.getElementById('keybindModalTitle');
const capturedShortcutText = document.getElementById('capturedShortcutText');
const clearCapturedShortcutBtn = document.getElementById('clearCapturedShortcutBtn');
const keybindActionSelect = document.getElementById('keybindActionSelect');
const keybindPressCountSelect = document.getElementById('keybindPressCountSelect');
const confirmKeybindBtn = document.getElementById('confirmKeybindBtn');
const cancelKeybindBtn = document.getElementById('cancelKeybindBtn');
const closeKeybindModal = document.getElementById('closeKeybindModal');

// OAuth DOM
const loginYtBtn = document.getElementById('loginYtBtn');
const logoutYtBtn = document.getElementById('logoutYtBtn');
const ytAuthStatus = document.getElementById('ytAuthStatus');
const ytUserInfo = document.getElementById('ytUserInfo');
const ytUserName = document.getElementById('ytUserName');
const myPlaylistsList = document.getElementById('myPlaylistsList');
const refreshMyPlaylistsBtn = document.getElementById('refreshMyPlaylistsBtn');
const sidebarLoginYtBtn = document.getElementById('sidebarLoginYtBtn');

// Confirmation Modal DOM
const confirmModal = document.getElementById('confirmModal');
const confirmMessage = document.getElementById('confirmMessage');
const cancelConfirmBtn = document.getElementById('cancelConfirmBtn');
const okConfirmBtn = document.getElementById('okConfirmBtn');
const closeConfirmModal = document.getElementById('closeConfirmModal');

// Sync Modal DOM
const syncModal = document.getElementById('syncModal');
const syncModalTitle = document.getElementById('syncModalTitle');
const syncModalDesc = document.getElementById('syncModalDesc');
const syncMatchCount = document.getElementById('syncMatchCount');
const syncAddCount = document.getElementById('syncAddCount');
const syncAddList = document.getElementById('syncAddList');
const syncRemoveCount = document.getElementById('syncRemoveCount');
const syncRemoveList = document.getElementById('syncRemoveList');
const syncReplaceCount = document.getElementById('syncReplaceCount');
const syncReplaceList = document.getElementById('syncReplaceList');
const syncReplaceGroup = document.getElementById('syncReplaceGroup');
const cancelSyncBtn = document.getElementById('cancelSyncBtn');
const applySyncBtn = document.getElementById('applySyncBtn');
const closeSyncModal = document.getElementById('closeSyncModal');
const repairLog = document.getElementById('repairLog');
const repairRetryButtons = document.getElementById('repairRetryButtons');
const retryTechRobo = document.getElementById('retryTechRobo');
const retryFilmot = document.getElementById('retryFilmot');
const retryWayback = document.getElementById('retryWayback');
const repairProgressFill = document.getElementById('repairProgressFill');
const repairStatus = document.getElementById('repairStatus');
const recoveryArea = document.getElementById('recoveryArea');
const brokenSongTitleOld = document.getElementById('brokenSongTitleOld');
const brokenSongTitleNew = document.getElementById('brokenSongTitleNew');
const candidatesGrid = document.getElementById('candidatesGrid');
const manualUrlInput = document.getElementById('manualUrlInput');
const submitManualUrl = document.getElementById('submitManualUrl');
const repairModal = document.getElementById('repairModal');
const closeRepairModal = document.getElementById('closeRepairModal');
const repairRenamedSummary = document.getElementById('repairRenamedSummary');
const openRenamedListBtn = document.getElementById('openRenamedListBtn');
const repairRenamedCount = document.getElementById('repairRenamedCount');
const repairBrokenSummary = document.getElementById('repairBrokenSummary');
const repairBrokenCount = document.getElementById('repairBrokenCount');
const startBrokenRepairBtn = document.getElementById('startBrokenRepairBtn');
const renamedTitlesModal = document.getElementById('renamedTitlesModal');
const closeRenamedTitlesModal = document.getElementById('closeRenamedTitlesModal');
const renamedTitlesMeta = document.getElementById('renamedTitlesMeta');
const renamedTitlesList = document.getElementById('renamedTitlesList');
const reopenRepairBtn = document.getElementById('reopenRepairBtn');

// New Controls
const volPlusBtn = document.getElementById('volPlusBtn');
const volMinusBtn = document.getElementById('volMinusBtn');
const addToPlaylistBtn = document.getElementById('addToPlaylistBtn');

// Now Playing Screen DOM
const nowPlayingScreen = document.getElementById('nowPlayingScreen');
const closeNowPlayingBtn = document.getElementById('closeNowPlayingBtn');
const npArtwork = document.getElementById('npArtwork');
const npBackground = document.getElementById('npBackground');
const npTitle = document.getElementById('npTitle');
const npArtist = document.getElementById('npArtist');
const npCurrentTime = document.getElementById('npCurrentTime');
const npTotalTime = document.getElementById('npTotalTime');
const npProgressFill = document.getElementById('npProgressFill');
const npProgressBar = document.getElementById('npProgressBar');
const npPrevBtn = document.getElementById('npPrevBtn');
const npNextBtn = document.getElementById('npNextBtn');
const npPlayBtn = document.getElementById('npPlayBtn');
const nowPlayingThumb = document.getElementById('nowPlayingThumb');

// Settings DOM
const blacklistWords = document.getElementById('settingBlacklistWords');
const blacklistChars = document.getElementById('settingBlacklistChars');
const autoThemeInput = document.getElementById('autoThemeInput');
const colorAccent = document.getElementById('colorAccent');
const colorAccentLight = document.getElementById('colorAccentLight');
const colorBg = document.getElementById('colorBg');
const colorSurface = document.getElementById('colorSurface');
const colorBorder = document.getElementById('colorBorder');
const colorTextPrimary = document.getElementById('colorTextPrimary');
const colorTextSecondary = document.getElementById('colorTextSecondary');
const saveThemeBtn = document.getElementById('saveThemeBtn');
const resetThemeBtn = document.getElementById('resetThemeBtn');

// Playlist Select Modal DOM
const playlistSelectModal = document.getElementById('playlistSelectModal');
const closePlaylistSelectModal = document.getElementById('closePlaylistSelectModal');
const cancelPlaylistSelectBtn = document.getElementById('cancelPlaylistSelectBtn');
const playlistSelectList = document.getElementById('playlistSelectList');

function getBuildStatusTag(versionInfo) {
  const isDev = !!versionInfo?.is_dev;
  const prefix = isDev ? 'd' : 'b';
  let build = String(versionInfo?.build || '0000-0000').trim();

  if (!build || build === '0000') {
    build = '0000-0000';
  }

  return `${prefix}${build}`;
}

function getThumbUrl(video) {
  if (video?.thumbnail) return video.thumbnail;
  if (video?.id) return `https://i.ytimg.com/vi/${video.id}/mqdefault.jpg`;
  return '';
}

function applyTheme(theme) {
  if (!theme) return;
  const root = document.documentElement;
  root.style.setProperty('--accent', theme.accent || '#7c3aed');
  root.style.setProperty('--accent-light', theme.accentLight || '#9f67ff');
  root.style.setProperty('--bg-base', theme.bg || '#0a0a0f');
  root.style.setProperty('--bg-surface', theme.surface || '#16161f');
  root.style.setProperty('--bg-elevated', theme.surface || '#16161f');
  root.style.setProperty('--border', theme.border || '#2d2d3d');
  root.style.setProperty('--text-primary', theme.textPrimary || '#f0f0f8');
  root.style.setProperty('--text-secondary', theme.textSecondary || '#9090b8');
}

function syncThemeInputs(theme) {
  if (!theme) return;
  if (colorAccent) colorAccent.value = theme.accent || '#7c3aed';
  if (colorAccentLight) colorAccentLight.value = theme.accentLight || '#9f67ff';
  if (colorBg) colorBg.value = theme.bg || '#0a0a0f';
  if (colorSurface) colorSurface.value = theme.surface || '#16161f';
  if (colorBorder) colorBorder.value = theme.border || '#2d2d3d';
  if (colorTextPrimary) colorTextPrimary.value = theme.textPrimary || '#f0f0f8';
  if (colorTextSecondary) colorTextSecondary.value = theme.textSecondary || '#9090b8';
}

function ensureSelectOption(selectEl, value, label) {
  if (!selectEl) return;
  const safeValue = String(value ?? 'default');
  const hasOption = Array.from(selectEl.options || []).some(opt => opt.value === safeValue);
  if (!hasOption) {
    const option = document.createElement('option');
    option.value = safeValue;
    option.textContent = label || safeValue;
    selectEl.appendChild(option);
  }
  selectEl.value = safeValue;
}

function populateSettingsUI() {
  if (settingPersistState) settingPersistState.checked = !!state.settings.persistState;
  if (settingCloseToTray) settingCloseToTray.checked = !!state.settings.closeToTray;
  if (settingShowVersion) settingShowVersion.checked = !!state.settings.showVersion;

  if (settingBlacklistWords) settingBlacklistWords.value = state.settings.blacklist_words || '';
  if (settingBlacklistChars) settingBlacklistChars.value = state.settings.blacklist_chars || '';
  if (autoThemeInput) autoThemeInput.value = state.settings.autoTheme || '';

  if (settingMaxDuration) settingMaxDuration.value = String(Number(state.settings.maxDuration ?? 600));
  if (settingMaxDurationEnabled) settingMaxDurationEnabled.checked = !!state.settings.maxDurationEnabled;

  if (settingVolMin) settingVolMin.value = String(Number(state.settings.volMin ?? 0));
  if (settingVolMax) settingVolMax.value = String(Number(state.settings.volMax ?? 1));

  if (settingRepairThreads) settingRepairThreads.value = String(Number(state.settings.repairThreads ?? 4));
  if (settingAutoSearchDelay) settingAutoSearchDelay.value = String(Number(state.settings.autoSearchDelay ?? 1.5));
  if (settingAutoMixSize) settingAutoMixSize.value = String(Number(state.settings.autoMixSize ?? 10));

  if (valRepairThreads) valRepairThreads.textContent = String(Number(state.settings.repairThreads ?? 4));
  if (valAutoSearchDelay) valAutoSearchDelay.textContent = String(Number(state.settings.autoSearchDelay ?? 1.5));
  if (valAutoMixSize) valAutoMixSize.textContent = String(Number(state.settings.autoMixSize ?? 10));

  ensureSelectOption(settingOutputDevice, state.settings.outputDevice || 'default', 'Predeterminado');
  ensureSelectOption(settingInputDevice, state.settings.inputDevice || 'default', 'Predeterminado');
}

function getVolumeBounds() {
  let min = Number(state.settings.volMin);
  let max = Number(state.settings.volMax);

  if (!Number.isFinite(min)) min = 0;
  if (!Number.isFinite(max)) max = 1;

  min = Math.max(0, Math.min(4.99, min));
  max = Math.max(0.01, Math.min(4.99, max));

  if (max <= min) {
    if (min >= 4.99) {
      min = 4.98;
      max = 4.99;
    } else {
      max = Math.min(4.99, min + 0.01);
    }
  }

  return { min, max };
}

function getEffectiveVolume(normalized) {
  const n = Math.max(0, Math.min(1, Number(normalized)));
  const { min, max } = getVolumeBounds();
  return min + (max - min) * n;
}

function applyShowVersionSetting() {
  if (!ytdlpStatus) return;
  ytdlpStatus.style.display = state.settings.showVersion ? '' : 'none';
}

function isWithinMaxDuration(video) {
  if (!state.settings.maxDurationEnabled) return true;

  const max = Number(state.settings.maxDuration);
  if (!Number.isFinite(max) || max <= 0) return true;

  const duration = Number(video?.duration);
  if (!Number.isFinite(duration) || duration <= 0) return true;

  return duration <= max;
}

function getActiveRepairId() {
  const ids = Object.keys(state.activeRepairs || {});
  for (const id of ids) {
    const repair = state.activeRepairs[id];
    if (repair && repair.status !== 'completed') return id;
  }
  return null;
}

function updateReopenRepairButton() {
  if (!reopenRepairBtn) return;

  const activeRepairId = getActiveRepairId();
  if (!activeRepairId || repairModal.style.display !== 'none') {
    reopenRepairBtn.style.display = 'none';
    return;
  }

  const repair = state.activeRepairs[activeRepairId];
  const checked = Number(repair?.checkedCount || 0);
  const total = Number(repair?.total || 0);
  reopenRepairBtn.textContent = total > 0
    ? `Reabrir reparacion (${checked}/${total})`
    : 'Reabrir reparacion';
  reopenRepairBtn.style.display = 'inline-flex';
}

function reopenActiveRepairModal() {
  const activeRepairId = getActiveRepairId();
  if (!activeRepairId) {
    if (reopenRepairBtn) reopenRepairBtn.style.display = 'none';
    showToast('No hay una reparacion activa.');
    return;
  }

  const repair = state.activeRepairs[activeRepairId];
  state.currentPlaylistId = activeRepairId;
  repairModal.style.display = 'flex';
  recoveryArea.style.display = 'none';

  const total = Number(repair?.total || 0);
  const checked = Number(repair?.checkedCount || 0);
  const pct = total > 0 ? (checked / total) * 100 : 0;
  repairProgressFill.style.width = `${pct}%`;
  repairStatus.textContent = total > 0
    ? `Verificando y actualizando titulos (${checked}/${total})...`
    : 'Reparacion en curso...';

  if (repair?.status === 'checking_done') {
    showRepairResults(activeRepairId);
  }

  updateReopenRepairButton();
}

async function refreshAudioDeviceOptions() {
  try {
    const devices = await invoke('list_audio_devices');
    const outputs = Array.isArray(devices?.outputs) ? devices.outputs : [];
    const inputs = Array.isArray(devices?.inputs) ? devices.inputs : [];

    if (settingOutputDevice) {
      settingOutputDevice.innerHTML = '';
      const def = document.createElement('option');
      def.value = 'default';
      def.textContent = 'Predeterminado';
      settingOutputDevice.appendChild(def);
      outputs.forEach((name) => {
        const option = document.createElement('option');
        option.value = String(name);
        option.textContent = String(name);
        settingOutputDevice.appendChild(option);
      });
      ensureSelectOption(settingOutputDevice, state.settings.outputDevice || 'default', `No disponible: ${state.settings.outputDevice || 'default'}`);
    }

    if (settingInputDevice) {
      settingInputDevice.innerHTML = '';
      const def = document.createElement('option');
      def.value = 'default';
      def.textContent = 'Predeterminado';
      settingInputDevice.appendChild(def);
      inputs.forEach((name) => {
        const option = document.createElement('option');
        option.value = String(name);
        option.textContent = String(name);
        settingInputDevice.appendChild(option);
      });
      ensureSelectOption(settingInputDevice, state.settings.inputDevice || 'default', `No disponible: ${state.settings.inputDevice || 'default'}`);
    }
  } catch (err) {
    console.warn('No se pudieron cargar dispositivos de audio:', err);
  }
}

async function applyAudioOutputDevice(silent = true) {
  if (!audio || typeof audio.setSinkId !== 'function') {
    return;
  }

  const selected = state.settings.outputDevice || 'default';
  try {
    await audio.setSinkId(selected);
  } catch (err) {
    console.warn('No se pudo aplicar output device:', err);

    if (selected !== 'default') {
      try {
        await audio.setSinkId('default');
        state.settings.outputDevice = 'default';
        if (settingOutputDevice) ensureSelectOption(settingOutputDevice, 'default', 'Predeterminado');
        await saveData();
        if (!silent) showToast('Dispositivo no encontrado. Se usara Predeterminado.');
        return;
      } catch (fallbackErr) {
        console.warn('No se pudo aplicar fallback a Predeterminado:', fallbackErr);
      }
    }

    if (!silent) showToast('No se pudo aplicar el dispositivo de salida', true);
  }
}

let sessionSaveTimeout = null;
let isAutoQueueFilling = false;
let autoQueueEnsureNextTs = 0;

function getAutoMinQueueTarget() {
  const value = Number(state.settings.autoMixSize ?? state.settings.autoMinQueue);
  if (!Number.isFinite(value)) return 10;
  return Math.max(1, Math.min(100, Math.floor(value)));
}

function getAutoMixSizeLimit() {
  const value = Number(state.settings.autoMixSize);
  if (!Number.isFinite(value)) return 10;
  return Math.max(1, Math.min(100, Math.floor(value)));
}

function getUpcomingQueueCount() {
  if (state.currentIndex < 0) return state.queue.length;
  return Math.max(0, state.queue.length - state.currentIndex - 1);
}

function trimConsumedQueueForAuto() {
  if (!state.settings.isAutoQueue) return false;
  if (state.settings.isShuffle) return false;
  if (Number(state.settings.repeatMode || 0) !== 0) return false;
  if (state.currentIndex <= 0) return false;

  state.queue.splice(0, state.currentIndex);
  state.currentIndex = 0;
  return true;
}

function updateAutoReferenceStatus({ query = '', searching = false } = {}) {
  if (!state.settings.isAutoQueue) {
    setSearchMode(state.searchMode, '');
    return;
  }

  const target = getAutoMinQueueTarget();
  const upcoming = getUpcomingQueueCount();
  const songLabel = target === 1 ? 'cancion' : 'canciones';
  const queueLabel = `AUTO: cola ${upcoming}/${target} (${songLabel} minimo)`;

  if (searching) {
    const basis = (query || '').trim() || 'tema actual';
    setSearchMode(state.searchMode, {
      text: `${queueLabel}. Por debajo del limite, buscando mix por: ${basis}`
    });
    return;
  }

  if (upcoming >= target) {
    setSearchMode(state.searchMode, `${queueLabel}. Aun se mantiene el limite de ${target} ${songLabel}.`);
    return;
  }

  const missing = Math.max(0, target - upcoming);
  setSearchMode(
    state.searchMode,
    `${queueLabel}. Faltan ${missing} para llegar al limite.`
  );
}

async function ensureAutoQueueSize(reason = 'auto') {
  if (!state.settings.isAutoQueue) return false;
  if (isAutoQueueFilling) return false;

  const target = getAutoMinQueueTarget();
  if (getUpcomingQueueCount() >= target) {
    updateAutoReferenceStatus();
    return false;
  }

  isAutoQueueFilling = true;
  let addedAny = false;

  try {
    const maxAttempts = Math.max(3, target * 2);
    let attempts = 0;

    while (getUpcomingQueueCount() < target && attempts < maxAttempts) {
      attempts += 1;
      const ok = await checkAutoQueue({ silent: true, autoTrigger: false });
      if (!ok) break;
      addedAny = true;
    }

    updateAutoReferenceStatus();

    return addedAny;
  } finally {
    isAutoQueueFilling = false;
    autoQueueEnsureNextTs = Date.now() + 12000;
  }
}

function updatePlaybackModeUI() {
  if (shuffleBtn) shuffleBtn.classList.toggle('active', !!state.settings.isShuffle);
  if (sidebarShuffleBtn) sidebarShuffleBtn.classList.toggle('active', !!state.settings.isShuffle);

  const repeatActive = Number(state.settings.repeatMode || 0) > 0;
  if (repeatBtn) {
    repeatBtn.classList.toggle('active', repeatActive);
    const badge = repeatBtn.querySelector('.repeat-one-badge');
    if (badge) badge.style.display = state.settings.repeatMode === 2 ? 'flex' : 'none';
  }
  if (sidebarRepeatBtn) {
    sidebarRepeatBtn.classList.toggle('active', repeatActive);
    const badge = sidebarRepeatBtn.querySelector('.repeat-one-badge');
    if (badge) badge.style.display = state.settings.repeatMode === 2 ? 'flex' : 'none';
  }

  if (autoBtn) autoBtn.classList.toggle('active', !!state.settings.isAutoQueue);
  if (sidebarAutoBtn) sidebarAutoBtn.classList.toggle('active', !!state.settings.isAutoQueue);
  updateAutoReferenceStatus();
}

async function savePlaybackSession() {
  try {
    const current = state.queue[state.currentIndex] || state.session.video;
    if (!current || !current.id) return;

    const safeQueue = state.queue
      .filter(v => v && v.id)
      .map(v => ({
        id: v.id,
        title: v.title || 'Unknown',
        thumbnail: getThumbUrl(v),
        uploader: v.uploader || v.channel || 'YouTube',
        duration: v.duration || null
      }));

    const payload = {
      video: {
        id: current.id,
        title: current.title || 'Unknown',
        thumbnail: getThumbUrl(current),
        uploader: current.uploader || current.channel || 'YouTube',
        duration: current.duration || null
      },
      time: Number(audio?.currentTime || 0),
      wasPlaying: !!state.isPlaying,
      queue: safeQueue,
      currentIndex: Number.isInteger(state.currentIndex) ? state.currentIndex : -1,
      playback: {
        isShuffle: !!state.settings.isShuffle,
        repeatMode: Number(state.settings.repeatMode || 0),
        isAutoQueue: !!state.settings.isAutoQueue,
        autoTheme: state.settings.autoTheme || ''
      }
    };

    state.session = { video: payload.video, time: payload.time };
    await invoke('save_data', { filename: 'session.json', data: payload });
  } catch (e) {
    console.warn('Error saving playback session:', e);
  }
}

function schedulePlaybackSessionSave(delayMs = 250) {
  if (sessionSaveTimeout) clearTimeout(sessionSaveTimeout);
  sessionSaveTimeout = setTimeout(() => {
    savePlaybackSession();
  }, delayMs);
}

async function restorePlaybackSession() {
  try {
    const session = await invoke('load_data', { filename: 'session.json' });
    if (!session || !session.video || !session.video.id) return;

    if (Array.isArray(session.queue)) {
      state.queue = session.queue
        .filter(v => v && v.id)
        .map(v => ({
          id: v.id,
          title: v.title || 'Unknown',
          thumbnail: getThumbUrl(v),
          uploader: v.uploader || v.channel || 'YouTube',
          duration: v.duration || null
        }));
    }

    if (session.playback && typeof session.playback === 'object') {
      state.settings.isShuffle = !!session.playback.isShuffle;
      state.settings.repeatMode = Number(session.playback.repeatMode || 0);
      state.settings.isAutoQueue = !!session.playback.isAutoQueue;
      if (typeof session.playback.autoTheme === 'string') {
        state.settings.autoTheme = session.playback.autoTheme;
        if (autoThemeInput) autoThemeInput.value = session.playback.autoTheme;
      }
      updatePlaybackModeUI();
    }

    const video = session.video;
    const savedTime = Math.max(0, Number(session.time || 0));
    const wasPlaying = !!session.wasPlaying;

    const desiredIndex = Number.isInteger(session.currentIndex) ? session.currentIndex : -1;
    if (desiredIndex >= 0 && desiredIndex < state.queue.length) {
      state.currentIndex = desiredIndex;
    } else {
      const existingIdx = state.queue.findIndex(v => v.id === video.id);
      if (existingIdx === -1) {
        state.queue.unshift(video);
        state.currentIndex = 0;
      } else {
        state.currentIndex = existingIdx;
      }
    }

    nowPlayingTitle.textContent = video.title || 'Sin reproducir';
    nowPlayingArtist.textContent = video.uploader || video.channel || 'YouTube';
    const thumb = getThumbUrl(video);
    nowPlayingImg.src = thumb;
    nowPlayingImg.style.display = thumb ? 'block' : 'none';
    npTitle.textContent = video.title || 'Sin reproducir';
    npArtist.textContent = video.uploader || video.channel || 'YouTube';
    npArtwork.src = thumb;
    npBackground.style.backgroundImage = `url(${thumb || ''})`;
    updateMediaSessionMetadata(video);

    const streamUrl = await invoke('get_stream_url', { videoId: video.id });
    if (!streamUrl) return;

    audio.src = streamUrl;
    audio.onloadedmetadata = () => {
      audio.currentTime = Math.min(savedTime, Math.max(0, (audio.duration || savedTime) - 0.25));
      updateProgressBar();
      if (wasPlaying) {
        audio.play().catch(() => {
          state.isPlaying = false;
          updatePlayButton();
        });
      }
      audio.onloadedmetadata = null;
    };

    state.session = { video, time: savedTime };
    updateQueueUI();
    updatePlayButton();
  } catch (e) {
    console.warn('Error restoring playback session:', e);
  }
}

function normalizePlaylistsData(raw) {
  if (!raw || typeof raw !== 'object') return {};

  // New format: { playlists: { id: { title, songs } } }
  if (raw.playlists && typeof raw.playlists === 'object') {
    return raw.playlists;
  }

  // Legacy format: { "PL...": { title, songs } }
  const values = Object.values(raw);
  const looksLegacy = values.some(v => v && typeof v === 'object' && Array.isArray(v.songs));
  return looksLegacy ? raw : {};
}

function normalizeFavoritesData(raw) {
  if (!raw || typeof raw !== 'object') return [];

  const source = Array.isArray(raw.favorites)
    ? raw.favorites
    : Array.isArray(raw.songs)
      ? raw.songs
      : [];

  const normalized = [];
  const seenIds = new Set();

  for (const item of source) {
    const favorite = cloneFavoriteSong(item);
    if (!favorite || seenIds.has(favorite.id)) continue;
    seenIds.add(favorite.id);
    normalized.push(favorite);
  }

  return normalized;
}

async function init() {
  installGlobalErrorLogging();
  appendSessionLog('[SESSION] Inicio de sesion');

  // Window controls
  const winMinimize = document.getElementById('winMinimize');
  const winMaximize = document.getElementById('winMaximize');
  const winClose = document.getElementById('winClose');
  if (winMinimize) winMinimize.onclick = () => invoke('app_minimize').catch(console.error);
  if (winMaximize) winMaximize.onclick = () => invoke('app_maximize').catch(console.error);
  if (winClose) {
    winClose.onclick = () => invoke('app_close', { closeToTray: !!state.settings.closeToTray }).catch(console.error);
  }

  // Verificación inicial de yt-dlp
  let ytdlpOk = false;
  try {
    ytdlpOk = await invoke('check_ytdlp');
    if (!ytdlpOk) {
      console.error('yt-dlp check failed');
      ytdlpStatus.className = 'status-badge status-error';
      statusText.innerText = 'yt-dlp Error';
    }
  } catch (err) {
    console.error('Error verificando yt-dlp:', err);
    ytdlpStatus.className = 'status-badge status-error';
    statusText.innerText = 'yt-dlp Error';
  }

  if (ytdlpOk) {
    ytdlpStatus.className = 'status-badge status-ok';
    statusText.innerText = getBuildStatusTag(state.versionInfo);
  }

  // Load settings
  try {
    const savedSettings = await invoke('load_data', { filename: 'settings.json' });
    if (savedSettings && Object.keys(savedSettings).length > 0) {
      Object.assign(state.settings, savedSettings);
    }
    applyShowVersionSetting();
  } catch (err) {
    console.error('Error loading settings:', err);
  }

  await refreshAudioDeviceOptions();
  await applyAudioOutputDevice(true);

  // Load playlists
  try {
    const savedPlaylists = await invoke('load_data', { filename: 'playlists.json' });
    state.playlists = normalizePlaylistsData(savedPlaylists);
  } catch (err) { }

  // Load favorites
  try {
    const savedFavorites = await invoke('load_data', { filename: 'favorites.json' });
    state.favorites = normalizeFavoritesData(savedFavorites);
  } catch (err) { }

  // Bind new buttons
  volPlusBtn.onclick = () => {
    setVolume(state.volume + 0.1);
    showToast(`Volumen: ${Math.round(state.volume * 100)}%`);
  };
  volMinusBtn.onclick = () => {
    setVolume(state.volume - 0.1);
    showToast(`Volumen: ${Math.round(state.volume * 100)}%`);
  };
  addToPlaylistBtn.onclick = () => {
    const current = state.queue[state.currentIndex];
    if (current) openPlaylistSelectModal(current);
    else showToast("No hay nada reproduciéndose", true);
  };

  closeNowPlayingBtn.onclick = () => {
    nowPlayingScreen.style.display = 'none';
  };

  npPrevBtn.onclick = playPrev;
  npNextBtn.onclick = playNext;
  npPlayBtn.onclick = () => playBtn.click();

  // Playlist Select Modal
  closePlaylistSelectModal.onclick = () => playlistSelectModal.style.display = 'none';
  cancelPlaylistSelectBtn.onclick = () => playlistSelectModal.style.display = 'none';

  // Global Shortcuts from Rust
  const listen = window.__TAURI__?.event?.listen;
  if (typeof listen === 'function') {
    listen('keybind-capture', (event) => {
      if (!keybindCaptureState.open) return;
      const shortcut = String(event?.payload || '').trim();
      if (!shortcut) return;
      keybindCaptureState.capturedShortcut = shortcut;
      setKeybindCaptureText(shortcut);
      showToast(`Capturado: ${shortcut}`);
    });
    listen('keybind-action', (event) => handleKeybindAction(event?.payload));
    listen('shortcut-play-pause', () => handleKeybindAction('play-pause'));
    listen('shortcut-next', () => handleKeybindAction('next-song'));
    listen('shortcut-prev', () => handleKeybindAction('previous-song'));
    listen('shortcut-vol-up', () => handleKeybindAction('volume-up'));
    listen('shortcut-vol-down', () => handleKeybindAction('volume-down'));
  }

  document.addEventListener('keydown', handleKeybindCaptureKeydown, true);
  setupMediaSessionControls();

  // Audio Events
  audio.ontimeupdate = () => {
    updateProgressBar();
    schedulePlaybackSessionSave(800);

    if (state.settings.isAutoQueue && !state.isAutoQueueSearching) {
      const now = Date.now();
      if (now >= autoQueueEnsureNextTs) {
        const remaining = Number.isFinite(audio.duration) ? (audio.duration - audio.currentTime) : Infinity;
        const target = getAutoMinQueueTarget();
        if (getUpcomingQueueCount() < target || remaining <= 45) {
          ensureAutoQueueSize('timeupdate');
        }
      }
    }
  };
  audio.onplay = () => {
    state.isPlaying = true;
    updatePlayButton();
    equalizer.classList.add('active');
    updateMediaSessionState();
    schedulePlaybackSessionSave(0);
    ensureAutoQueueSize('play');
  };
  audio.onpause = () => {
    state.isPlaying = false;
    updatePlayButton();
    equalizer.classList.remove('active');
    updateMediaSessionState();
    schedulePlaybackSessionSave(0);
  };
  audio.onended = async () => {
    state.isPlaying = false;
    updatePlayButton();
    equalizer.classList.remove('active');
    updateMediaSessionState();
    schedulePlaybackSessionSave(0);
    if (state.settings.repeatMode === 2) { // Repeat one
      playFromQueue(state.currentIndex);
    } else {
      if (state.currentIndex < state.queue.length - 1) {
        playNext();
      } else if (state.settings.isAutoQueue) {
        await checkAutoQueue();
      } else if (state.settings.repeatMode === 1) {
        playNext();
      }
    }
  };
  audio.onerror = async () => {
    const mediaCode = audio?.error?.code ? `media_code_${audio.error.code}` : 'media_unknown';
    const current = state.queue[state.currentIndex] || state.session.video;
    await handlePlaybackFailure(current, 'audio.onerror', mediaCode);
  };

  // Progress Click
  progressBar.onclick = (e) => {
    if (!audio.duration) return;
    const rect = progressBar.getBoundingClientRect();
    const pct = (e.clientX - rect.left) / rect.width;
    audio.currentTime = pct * audio.duration;
  };

  npProgressBar.onclick = (e) => {
    if (!audio.duration) return;
    const rect = npProgressBar.getBoundingClientRect();
    const pct = (e.clientX - rect.left) / rect.width;
    audio.currentTime = pct * audio.duration;
  };

  // Volume controls (click + drag)
  const setVolumeFromClientX = (clientX) => {
    if (!volumeBar) return;
    const rect = volumeBar.getBoundingClientRect();
    const pct = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
    setVolume(pct);
  };

  if (volumeBar) {
    volumeBar.onclick = (e) => setVolumeFromClientX(e.clientX);
    volumeBar.onmousedown = (e) => {
      setVolumeFromClientX(e.clientX);
      const onMove = (moveEvent) => setVolumeFromClientX(moveEvent.clientX);
      const onUp = () => {
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
      };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    };
  }

  if (muteBtn) {
    muteBtn.onclick = () => {
      state.isMuted = !state.isMuted;
      audio.muted = state.isMuted;
      muteBtn.classList.toggle('active', state.isMuted);
    };
  }

  // Sidebar Toggles
  queueToggleBtn.onclick = () => {
    queueSidebar.classList.toggle('open');
    queueToggleBtn.classList.toggle('active');
  };
  playlistToggleBtn.onclick = () => {
    playlistSidebar.classList.toggle('open');
    playlistToggleBtn.classList.toggle('active');
  };

  // Nav Buttons
  document.querySelectorAll('.logo').forEach(el => el.onclick = () => showScreen('welcome'));

  if (showSettingsBtn) showSettingsBtn.onclick = () => showScreen('settings');
  if (showCustomBtn) showCustomBtn.onclick = () => showScreen('custom');
  if (showBindsBtn) showBindsBtn.onclick = () => showScreen('binds');

  if (localPlaylist) localPlaylist.onclick = () => selectPlaylist('local');
  const historyPlaylist = document.getElementById('historyPlaylist');
  if (historyPlaylist) historyPlaylist.onclick = () => selectPlaylist('history');

  if (importPlaylistBtn) {
    importPlaylistBtn.onclick = () => {
      const isVisible = playlistImportArea && playlistImportArea.style.display !== 'none';
      if (playlistImportArea) playlistImportArea.style.display = isVisible ? 'none' : 'block';
    };
  }

  if (confirmImportBtn) {
    confirmImportBtn.onclick = async () => {
      const url = (playlistUrlInput ? playlistUrlInput.value : '').trim();
      if (!url) {
        showToast('Pega una URL de playlist', true);
        return;
      }
      try {
        const data = await invoke('fetch_playlist', { url });
        const playlistId = extractPlaylistId(url) || data.id || `local_${Date.now()}`;
        const importSource = String(data?.source || 'ytdlp').toLowerCase();

        if (importSource === 'wayback') {
          const proceed = await showConfirm('La playlist parece borrada en YouTube. Se intentara importar desde Wayback y recuperar titulos automaticamente. ¿Continuar?');
          if (!proceed) {
            showToast('Importacion cancelada');
            return;
          }
        }

        const entries = normalizePlaylistEntries(data);

        if (entries.length === 0) {
          state.playlists[playlistId] = {
            id: playlistId,
            title: data.title || `Playlist ${playlistId}`,
            sourceUrl: url,
            songs: []
          };

          await saveData();
          renderPlaylists();
          selectPlaylist(playlistId);

          if (playlistImportArea) playlistImportArea.style.display = 'none';
          if (playlistUrlInput) playlistUrlInput.value = '';
          showToast('Playlist creada sin canciones recuperables');
          return;
        }

        const recoverCandidates = entries
          .map((song, idx) => ({ song, idx }))
          .filter(({ song }) => needsTitleRecovery(song.title));

        if (recoverCandidates.length > 0) {
          showToast(`Recuperando títulos (${recoverCandidates.length})...`);
          const parallel = Math.max(1, Math.min(8, Number(state.settings.repairThreads) || 4));
          await recoverTitlesBatch(entries, recoverCandidates, parallel);
        }

        state.playlists[playlistId] = {
          id: playlistId,
          title: data.title || `Playlist ${playlistId}`,
          sourceUrl: url,
          songs: entries
        };

        await saveData();
        renderPlaylists();
        selectPlaylist(playlistId);

        if (playlistImportArea) playlistImportArea.style.display = 'none';
        if (playlistUrlInput) playlistUrlInput.value = '';
        if (importSource === 'wayback') {
          showToast('Playlist recuperada e importada');
        } else {
          showToast('Playlist importada');
        }
      } catch (err) {
        const msg = err?.message || String(err);
        showToast(`Error importando playlist: ${msg}`, true);
      }
    };
  }

  if (saveSettingsBtn) {
    saveSettingsBtn.onclick = async () => {
      state.settings.persistState = !!settingPersistState?.checked;
      state.settings.closeToTray = !!settingCloseToTray?.checked;
      state.settings.showVersion = !!settingShowVersion?.checked;
      state.settings.outputDevice = settingOutputDevice?.value || 'default';
      state.settings.inputDevice = settingInputDevice?.value || 'default';
      state.settings.blacklist_words = settingBlacklistWords?.value || '';
      state.settings.blacklist_chars = settingBlacklistChars?.value || '';
      state.settings.maxDuration = Number(settingMaxDuration?.value || 600);
      state.settings.maxDurationEnabled = !!settingMaxDurationEnabled?.checked;
      state.settings.volMin = Number(settingVolMin?.value || 0);
      state.settings.volMax = Number(settingVolMax?.value || 1);
      state.settings.repairThreads = Number(settingRepairThreads?.value || 4);
      state.settings.autoSearchDelay = Number(settingAutoSearchDelay?.value || 1.5);
      state.settings.autoMixSize = Number(settingAutoMixSize?.value || 10);
      state.settings.autoTheme = autoThemeInput?.value || '';

      state.settings.maxDuration = Math.max(1, Math.floor(Number(state.settings.maxDuration) || 600));
      state.settings.repairThreads = Math.max(1, Math.min(10, Math.floor(Number(state.settings.repairThreads) || 4)));
      state.settings.autoSearchDelay = Math.max(0.25, Math.min(5, Number(state.settings.autoSearchDelay) || 1.5));
      state.settings.autoMixSize = Math.max(1, Math.min(100, Math.floor(Number(state.settings.autoMixSize) || 10)));

      const bounds = getVolumeBounds();
      state.settings.volMin = bounds.min;
      state.settings.volMax = bounds.max;

      if (valRepairThreads) valRepairThreads.textContent = String(state.settings.repairThreads);
      if (valAutoSearchDelay) valAutoSearchDelay.textContent = String(state.settings.autoSearchDelay);
      if (valAutoMixSize) valAutoMixSize.textContent = String(state.settings.autoMixSize);

      if (settingMaxDuration) settingMaxDuration.value = String(state.settings.maxDuration);
      if (settingVolMin) settingVolMin.value = String(Number(state.settings.volMin.toFixed(3)));
      if (settingVolMax) settingVolMax.value = String(Number(state.settings.volMax.toFixed(3)));
      if (settingRepairThreads) settingRepairThreads.value = String(state.settings.repairThreads);
      if (settingAutoSearchDelay) settingAutoSearchDelay.value = String(state.settings.autoSearchDelay);
      if (settingAutoMixSize) settingAutoMixSize.value = String(state.settings.autoMixSize);

      applyShowVersionSetting();
      await applyAudioOutputDevice(false);
      setVolume(state.volume);

      state.settings.keybinds = normalizeKeybinds(state.settings.keybinds);
      await saveData();
      await syncKeybindsToBackend();
      updatePlaybackModeUI();
      schedulePlaybackSessionSave(0);
      showToast('Ajustes guardados');
    };
  }

  if (saveThemeBtn) {
    saveThemeBtn.onclick = async () => {
      state.settings.theme = {
        accent: colorAccent?.value || '#7c3aed',
        accentLight: colorAccentLight?.value || '#9f67ff',
        bg: colorBg?.value || '#0a0a0f',
        surface: colorSurface?.value || '#16161f',
        border: colorBorder?.value || '#2d2d3d',
        textPrimary: colorTextPrimary?.value || '#f0f0f8',
        textSecondary: colorTextSecondary?.value || '#9090b8'
      };
      applyTheme(state.settings.theme);
      await saveData();
      showToast('Tema aplicado');
    };
  }

  if (resetThemeBtn) {
    resetThemeBtn.onclick = async () => {
      state.settings.theme = {
        accent: '#7c3aed',
        accentLight: '#9f67ff',
        bg: '#0a0a0f',
        surface: '#16161f',
        border: '#2d2d3d',
        textPrimary: '#f0f0f8',
        textSecondary: '#9090b8'
      };
      syncThemeInputs(state.settings.theme);
      applyTheme(state.settings.theme);
      await saveData();
      showToast('Tema restablecido');
    };
  }

  if (settingRepairThreads) {
    settingRepairThreads.oninput = () => {
      if (valRepairThreads) valRepairThreads.textContent = settingRepairThreads.value;
    };
  }
  if (settingAutoSearchDelay) {
    settingAutoSearchDelay.oninput = () => {
      if (valAutoSearchDelay) valAutoSearchDelay.textContent = settingAutoSearchDelay.value;
    };
  }

  if (settingAutoMixSize) {
    settingAutoMixSize.oninput = () => {
      if (valAutoMixSize) valAutoMixSize.textContent = settingAutoMixSize.value;
    };

    settingAutoMixSize.addEventListener('wheel', (event) => {
      event.preventDefault();
      const step = Number(settingAutoMixSize.step || 1) || 1;
      const min = Number(settingAutoMixSize.min || 1) || 1;
      const max = Number(settingAutoMixSize.max || 100) || 100;
      const current = Number(settingAutoMixSize.value || 10) || 10;
      const next = event.deltaY < 0 ? current + step : current - step;
      settingAutoMixSize.value = String(Math.max(min, Math.min(max, next)));
      if (valAutoMixSize) valAutoMixSize.textContent = settingAutoMixSize.value;
    }, { passive: false });
  }

  if (addKeybindBtn) {
    addKeybindBtn.onclick = () => openKeybindCaptureModal();
  }

  if (cancelKeybindBtn) {
    cancelKeybindBtn.onclick = () => closeKeybindCaptureModal();
  }

  if (closeKeybindModal) {
    closeKeybindModal.onclick = () => closeKeybindCaptureModal();
  }

  if (clearCapturedShortcutBtn) {
    clearCapturedShortcutBtn.onclick = () => {
      keybindCaptureState.capturedShortcut = '';
      setKeybindCaptureText('Esperando captura...');
    };
  }

  if (confirmKeybindBtn) {
    confirmKeybindBtn.onclick = () => commitKeybindFromModal();
  }

  // Search
  searchInput.onkeydown = (e) => {
    if (e.key === 'Enter') search(searchInput.value);
    else setTimeout(() => getSuggestions(searchInput.value), 10);
  };
  document.querySelector('.search-submit').onclick = () => search(searchInput.value);

  if (autoThemeInput) {
    autoThemeInput.onkeydown = async (e) => {
      if (e.key !== 'Enter') return;
      e.preventDefault();

      const query = (autoThemeInput.value || '').trim();
      if (!query) return;

      state.settings.autoTheme = query;
      setSearchMode(state.searchMode, { text: `AUTO manual: buscando "${query}"` });
      await performQuickSearch(query, {
        enqueueOnly: false,
        replaceQueue: true,
        silent: false
      });
    };
  }

  // Playback Controls
  playBtn.onclick = () => togglePlay();
  npPlayBtn.onclick = () => togglePlay();
  nextBtn.onclick = () => playNext();
  npNextBtn.onclick = () => playNext();
  prevBtn.onclick = () => playPrev();
  npPrevBtn.onclick = () => playPrev();

  shuffleBtn.onclick = () => {
    state.settings.isShuffle = !state.settings.isShuffle;
    updatePlaybackModeUI();
    schedulePlaybackSessionSave(0);
    saveData();
  };

  repeatBtn.onclick = () => {
    state.settings.repeatMode = (state.settings.repeatMode + 1) % 3;
    updatePlaybackModeUI();
    schedulePlaybackSessionSave(0);
    saveData();
  };

  if (sidebarShuffleBtn) {
    sidebarShuffleBtn.onclick = () => {
      state.settings.isShuffle = !state.settings.isShuffle;
      updatePlaybackModeUI();
      schedulePlaybackSessionSave(0);
      saveData();
    };
  }

  if (sidebarRepeatBtn) {
    sidebarRepeatBtn.onclick = () => {
      state.settings.repeatMode = (state.settings.repeatMode + 1) % 3;
      updatePlaybackModeUI();
      schedulePlaybackSessionSave(0);
      saveData();
    };
  }

  const toggleAutoQueueFromButton = () => {
    state.settings.isAutoQueue = !state.settings.isAutoQueue;
    updatePlaybackModeUI();
    schedulePlaybackSessionSave(0);
    saveData();
    showToast(state.settings.isAutoQueue ? 'AUTO cola activado' : 'AUTO cola desactivado');
    if (state.settings.isAutoQueue) {
      ensureAutoQueueSize('toggle');
    }
  };

  if (autoBtn) autoBtn.onclick = toggleAutoQueueFromButton;
  if (sidebarAutoBtn) sidebarAutoBtn.onclick = toggleAutoQueueFromButton;

  if (clearQueueBtn) {
    clearQueueBtn.onclick = () => {
      state.queue = [];
      state.currentIndex = -1;
      updateQueueUI();
      schedulePlaybackSessionSave(0);
      showToast('Cola limpiada');
    };
  }

  // Now Playing Screen Toggle
  nowPlayingThumb.onclick = () => nowPlayingScreen.style.display = 'flex';
  nowPlayingTitle.onclick = () => nowPlayingScreen.style.display = 'flex';
  closeNowPlayingBtn.onclick = () => nowPlayingScreen.style.display = 'none';

  // Favorites
  heartBtn.onclick = () => {
    const current = state.queue[state.currentIndex];
    if (!current) return;
    const idx = state.favorites.findIndex(f => f.id === current.id);
    if (idx === -1) {
      void addSongToFavorites(current, { updateCurrentHeart: true, playSound: false });
    } else {
      state.favorites.splice(idx, 1);
      updatePlayButton();
      showToast("Quitado de favoritos");
      void saveData();
    }
  };

  // Blacklist Sync
  if (blacklistWords) {
    blacklistWords.oninput = () => {
      state.settings.blacklist_words = blacklistWords.value;
      saveData();
    };
  }
  if (blacklistChars) {
    blacklistChars.oninput = () => {
      state.settings.blacklist_chars = blacklistChars.value;
      saveData();
    };
  }

  // YouTube OAuth assignments
  if (loginYtBtn) {
    loginYtBtn.onclick = async () => {
      showToast('Iniciando sesión en el navegador...');
      try {
        const tokenData = await invoke('start_oauth_flow');
        if (tokenData) {
          showToast('Login exitoso');
          checkAuthStatus();
        }
      } catch (err) {
        showToast('Error de login: ' + err, true);
      }
    };
  }

  if (logoutYtBtn) {
    logoutYtBtn.onclick = async () => {
      try {
        await invoke('save_data', { filename: 'auth.json', data: {} });
        checkAuthStatus();
        showToast('Sesión cerrada');
      } catch (e) { console.error(e); }
    };
  }

  if (refreshMyPlaylistsBtn) {
    refreshMyPlaylistsBtn.onclick = () => fetchMyPlaylists();
  }

  // Add to Playlist
  if (addToPlaylistBtn) {
    addToPlaylistBtn.onclick = () => {
      const current = state.queue[state.currentIndex];
      if (current) openPlaylistSelectModal(current);
    };
  }

  // Additional global shortcuts
  if (typeof listen === 'function') {
    listen('shortcut-vol-down', () => {
      setVolume(state.volume - 0.1);
      showToast(`Volumen: ${Math.round(state.volume * 100)}%`);
    });
  }

  // Initial Load
  loadInitialData();
  updatePlaybackModeUI();

  // Persist session when app goes background/close
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'visible') {
      schedulePlaybackSessionSave(0);
    }
  });
  window.addEventListener('beforeunload', () => {
    schedulePlaybackSessionSave(0);
  });
}

async function loadInitialData() {
  try {
    const settings = await invoke('load_data', { filename: 'settings.json' });
    let needsSettingsSave = false;
    if (settings && typeof settings === 'object') {
      state.settings = { ...state.settings, ...settings };
      const originalKeybindCount = Array.isArray(state.settings.keybinds) ? state.settings.keybinds.length : 0;
      const normalizedKeybinds = normalizeKeybinds(state.settings.keybinds);
      if (!Array.isArray(state.settings.keybinds) || originalKeybindCount === 0) {
        state.settings.keybinds = getDefaultKeybinds();
        needsSettingsSave = true;
      } else {
        state.settings.keybinds = normalizedKeybinds;
        if (normalizedKeybinds.length !== originalKeybindCount) {
          needsSettingsSave = true;
        }
        if (state.settings.keybinds.length === 0) {
          state.settings.keybinds = getDefaultKeybinds();
          needsSettingsSave = true;
        }
      }
      setVolume(state.settings.volume ?? 0.5);
      applyTheme(state.settings.theme);
      syncThemeInputs(state.settings.theme);
      applyShowVersionSetting();
      await refreshAudioDeviceOptions();
      await applyAudioOutputDevice(true);
      await loadNotificationSoundAsset();
    }

    populateSettingsUI();
  populateKeybindActionSelect();
  renderKeybindsList();
    updatePlaybackModeUI();
  await syncKeybindsToBackend();
    if (needsSettingsSave) await saveData();

    const plData = await invoke('load_data', { filename: 'playlists.json' });
    state.playlists = normalizePlaylistsData(plData);

    const favData = await invoke('load_data', { filename: 'favorites.json' });
    state.favorites = normalizeFavoritesData(favData);

    const histData = await invoke('load_data', { filename: 'history.json' });
    if (histData && histData.history) state.history = histData.history;

    renderPlaylists();
    await restorePlaybackSession();
    ensureAutoQueueSize('initial-load');
  } catch (e) {
    console.warn('Error loading initial data:', e);
  }
}

function togglePlay() {
  if (!audio.src) return;
  if (state.isPlaying) audio.pause();
  else audio.play();
}

async function checkAutoQueue(options = {}) {
  const { silent = false, autoTrigger = true } = options;
  if (!state.settings.isAutoQueue) return false;
  if (state.isAutoQueueSearching) return false;

  const target = getAutoMinQueueTarget();
  if (getUpcomingQueueCount() >= target) {
    updateAutoReferenceStatus();
    return false;
  }

  state.isAutoQueueSearching = true;
  try {
    const currentVideo = state.queue[state.currentIndex] || state.session.video;
    const currentVideoId = getEntryVideoId(currentVideo);
    const currentTitle = (currentVideo?.title || '').trim() || 'tema actual';

    if (!currentVideoId) {
      if (!silent) showToast('AUTO: no se pudo detectar el video actual para generar mix', true);
      updateAutoReferenceStatus();
      return false;
    }

    const delayMs = Math.max(0, Number(state.settings.autoSearchDelay || 0) * 1000);
    if (delayMs > 0) {
      await new Promise(resolve => setTimeout(resolve, delayMs));
    }

    updateAutoReferenceStatus({ query: currentTitle, searching: true });
    const missing = Math.max(1, target - getUpcomingQueueCount());
    const ok = await performAutoMixSearch(currentVideoId, {
      enqueueOnly: true,
      autoTrigger,
      silent,
      mixSize: Math.max(getAutoMixSizeLimit(), missing),
      desiredCount: missing
    });

    if (ok) {
      updateAutoReferenceStatus();
      return true;
    }

    if (!silent) showToast('AUTO: no se encontraron resultados validos en el mix actual', true);
    updateAutoReferenceStatus();
    return false;
  } finally {
    state.isAutoQueueSearching = false;
  }
}

// ===== NAVIGATION & UI =====
function showScreen(screenId) {
  state.currentScreen = screenId;
  welcomeScreen.style.display = screenId === 'welcome' ? 'flex' : 'none';
  loadingScreen.style.display = screenId === 'loading' ? 'flex' : 'none';
  resultsGrid.style.display = (screenId === 'results' || screenId === 'playlist') ? 'block' : 'none';
  errorScreen.style.display = screenId === 'error' ? 'flex' : 'none';
  settingsScreen.style.display = screenId === 'settings' ? 'block' : 'none';
  customizationScreen.style.display = screenId === 'custom' ? 'block' : 'none';
  if (bindsScreen) bindsScreen.style.display = screenId === 'binds' ? 'block' : 'none';

  if (screenId !== 'playlist' && resultsActions) {
    resultsActions.innerHTML = '';
  }

  // Toggle active nav buttons
  showSettingsBtn.classList.toggle('active', screenId === 'settings');
  showCustomBtn.classList.toggle('active', screenId === 'custom');
  if (showBindsBtn) showBindsBtn.classList.toggle('active', screenId === 'binds');
}

function renderHeaderActions() {
  const container = resultsActions;
  if (!container) return;
  container.innerHTML = '';

  if (state.currentScreen !== 'playlist' || !state.currentPlaylistId) return;

  const canRenamePlaylist = canManagePlaylistMetadata(state.currentPlaylistId);
  const renameBtnHtml = canRenamePlaylist
    ? `<button class="header-manage-btn header-manage-icon-btn icon-rename" id="playlistRenameBtn" title="Renombrar playlist" aria-label="Renombrar playlist">
        <svg viewBox="0 0 24 24" fill="none"><path d="M4 20h4l10.5-10.5-4-4L4 16v4z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M12.5 6.5l4 4" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>
      </button>`
    : '';

  const btn = document.createElement('div');
  btn.className = 'header-action-btn management-actions';
  btn.innerHTML = `
    <div class="mgmt-actions-left">
      <button class="header-manage-btn header-manage-icon-btn icon-play" id="playlistPlayBtn" title="Reproducir playlist (limpia cola)" aria-label="Reproducir playlist">
        <svg viewBox="0 0 24 24" fill="none"><path d="M6 4v16l13-8-13-8z" fill="currentColor"/></svg>
      </button>
      <button class="header-manage-btn header-manage-icon-btn icon-add" id="playlistAddBtn" title="Anadir playlist a la cola" aria-label="Anadir playlist a la cola">
        <svg viewBox="0 0 24 24" fill="none"><path d="M12 5v14M5 12h14" stroke="currentColor" stroke-width="2.8" stroke-linecap="round"/></svg>
      </button>
    </div>
    <div class="mgmt-actions-right">
      ${renameBtnHtml}
      <button class="header-manage-btn header-manage-icon-btn icon-repair" id="repairPlaylistBtn" title="Reparar playlist" aria-label="Reparar playlist">
        <svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M14.7 6.3a4 4 0 0 0-4.9 4.9L4 17v3h3l5.8-5.8a4 4 0 0 0 4.9-4.9l-2.3 2.3a1 1 0 0 1-1.4 0l-1.2-1.2a1 1 0 0 1 0-1.4z" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
      </button>
      <button class="header-manage-btn header-manage-icon-btn header-manage-btn-danger icon-delete" id="playlistDeleteBtn" title="Borrar playlist" aria-label="Borrar playlist">
        <svg viewBox="0 0 24 24" fill="none"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
      </button>
      <button class="header-manage-btn header-manage-icon-btn icon-sync" id="updatePlaylistBtn" title="Sincronizar playlist" aria-label="Sincronizar playlist">
        <svg viewBox="0 0 24 24" fill="none"><path d="M20 6v5h-5M4 18v-5h5" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/><path d="M18.5 11A7 7 0 0 0 7 7.5L4 10M5.5 13A7 7 0 0 0 17 16.5L20 14" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/></svg>
      </button>
    </div>
  `;
  container.appendChild(btn);

  const playBtn = document.getElementById('playlistPlayBtn');
  if (playBtn) playBtn.onclick = () => queuePlaylistFromSidebar(state.currentPlaylistId, { replaceQueue: true });

  const addBtn = document.getElementById('playlistAddBtn');
  if (addBtn) addBtn.onclick = () => queuePlaylistFromSidebar(state.currentPlaylistId, { replaceQueue: false });

  const renameBtn = document.getElementById('playlistRenameBtn');
  if (renameBtn) renameBtn.onclick = () => renameCurrentPlaylist();

  const deleteBtn = document.getElementById('playlistDeleteBtn');
  if (deleteBtn) deleteBtn.onclick = () => deleteCurrentPlaylist();

  const repairBtn = document.getElementById('repairPlaylistBtn');
  if (repairBtn) repairBtn.onclick = () => startPlaylistRepair(state.currentPlaylistId);

  const updateBtn = document.getElementById('updatePlaylistBtn');
  if (updateBtn) updateBtn.onclick = () => updateCurrentPlaylist();
}

// ===== PLAYLIST LOGIC =====
function renderPlaylists() {
  const list = importedPlaylistsList;
  if (!list) return;
  list.innerHTML = '';

  Object.keys(state.playlists).forEach(id => {
    const pl = state.playlists[id];
    const item = document.createElement('div');
    item.className = `playlist-item ${state.currentPlaylistId === id ? 'active' : ''}`;
    item.innerHTML = `
      <svg viewBox="0 0 24 24" fill="none"><path d="M8 6h13M8 12h13M8 18h7M3 6h.01M3 12h.01M3 18h.01" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
      <span class="playlist-name">${escapeHTML(pl.title)}</span>
      <div class="playlist-actions">
        <button class="playlist-action-btn play-pl" title="Reproducir playlist (limpia cola)">
          <svg viewBox="0 0 24 24" fill="none"><path d="M6 4v16l13-8-13-8z" fill="currentColor"/></svg>
        </button>
        <button class="playlist-action-btn add-pl" title="Anadir playlist a la cola">
          <svg viewBox="0 0 24 24" fill="none"><path d="M12 5v14M5 12h14" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
        </button>
      </div>
    `;
    item.onclick = (e) => {
      if (e.target.closest('.play-pl')) {
        e.stopPropagation();
        queuePlaylistFromSidebar(id, { replaceQueue: true });
        return;
      }
      if (e.target.closest('.add-pl')) {
        e.stopPropagation();
        queuePlaylistFromSidebar(id, { replaceQueue: false });
        return;
      }
      selectPlaylist(id);
    };
    list.appendChild(item);
  });

  if (localPlaylist) {
    localPlaylist.className = `playlist-item ${state.currentPlaylistId === 'local' ? 'active' : ''}`;
  }
  const historyPlaylist = document.getElementById('historyPlaylist');
  if (historyPlaylist) {
    historyPlaylist.className = `playlist-item ${state.currentPlaylistId === 'history' ? 'active' : ''}`;
  }
}

function getSongsForPlaylistId(id) {
  if (id === 'local') return state.favorites || [];
  if (id === 'history') return state.history || [];
  if (state.playlists[id] && Array.isArray(state.playlists[id].songs)) return state.playlists[id].songs;
  if (id && id.toString().startsWith('PL') && Array.isArray(state.lastSearchResults)) return state.lastSearchResults;
  return [];
}

function canManageSongActions() {
  return (
    !!state.currentPlaylistId
    && resultsGrid
    && resultsGrid.style.display !== 'none'
    && state.currentScreen === 'playlist'
  );
}

function playSongNow(song) {
  if (!song || !song.id) return;
  state.queue = [{ ...song }];
  state.currentIndex = 0;
  updateQueueUI();
  playFromQueue(0);
}

async function queuePlaylistFromSidebar(playlistId, options = {}) {
  const { replaceQueue = false } = options;
  const songs = getSongsForPlaylistId(playlistId).filter(Boolean);
  if (!songs.length) {
    showToast('La playlist no tiene canciones', true);
    return;
  }

  if (replaceQueue) {
    state.queue = songs.map((song) => ({ ...song }));
    state.currentIndex = -1;
    updateQueueUI();
    playFromQueue(0);
    showToast(`Reproduciendo playlist (${songs.length} canciones)`);
    return;
  }

  songs.forEach((song) => {
    addToQueue(song, {
      allowDuplicate: true,
      showDuplicateToast: false,
      showAddedToast: false
    });
  });
  showToast(`Anadidas ${songs.length} canciones a la cola`);
}

function canManagePlaylistMetadata(playlistId) {
  return !!playlistId && !!state.playlists[playlistId];
}

async function renameCurrentPlaylist() {
  const playlistId = state.currentPlaylistId;
  if (!canManagePlaylistMetadata(playlistId)) {
    showToast('Renombrar solo esta disponible para playlists importadas', true);
    return;
  }

  const current = state.playlists[playlistId];
  const nextTitle = window.prompt('Nuevo nombre de playlist:', current.title || `Playlist ${playlistId}`);
  if (nextTitle === null) return;
  const clean = (nextTitle || '').trim();
  if (!clean) {
    showToast('El nombre no puede estar vacio', true);
    return;
  }

  current.title = clean;
  await saveData();
  renderPlaylists();
  await selectPlaylist(playlistId);
  showToast('Playlist renombrada');
}

async function deleteCurrentPlaylist() {
  const playlistId = state.currentPlaylistId;
  if (!canManagePlaylistMetadata(playlistId)) {
    showToast('Borrar solo esta disponible para playlists importadas', true);
    return;
  }

  const playlist = state.playlists[playlistId];
  const confirmed = await showConfirm(`Eliminar playlist "${playlist?.title || playlistId}"?`);
  if (!confirmed) return;

  delete state.playlists[playlistId];
  await saveData();
  renderPlaylists();
  await selectPlaylist('local');
  showToast('Playlist eliminada');
}

async function updateCurrentPlaylist() {
  const playlistId = state.currentPlaylistId;
  if (!playlistId) return;

  if (state.playlists[playlistId]?.sourceUrl) {
    try {
      showToast('Actualizando playlist...');
      const data = await invoke('fetch_playlist', { url: state.playlists[playlistId].sourceUrl });
      const entries = normalizePlaylistEntries(data);
      state.playlists[playlistId] = {
        ...state.playlists[playlistId],
        title: data.title || state.playlists[playlistId].title,
        songs: entries
      };
      await saveData();
      await selectPlaylist(playlistId);
      showToast('Playlist actualizada');
    } catch (err) {
      showToast(`No se pudo actualizar: ${err}`, true);
    }
    return;
  }

  if (playlistId.toString().startsWith('PL')) {
    const title = resultsTitle?.textContent || `Playlist ${playlistId}`;
    await selectYtPlaylist(playlistId, title);
    showToast('Playlist actualizada');
    return;
  }

  await selectPlaylist(playlistId);
  showToast('Vista actualizada');
}

async function editSongTitleAtIndex(playlistId, index) {
  const songs = getSongsForPlaylistId(playlistId);
  const song = songs[index];
  if (!song) return;

  const currentTitle = song.title || `Video ${song.id}`;
  const nextTitle = window.prompt('Nuevo titulo:', currentTitle);
  if (nextTitle === null) return;

  const clean = normalizeRecoveredTitle(nextTitle);
  if (!clean) {
    showToast('El titulo no puede estar vacio', true);
    return;
  }
  if (clean === currentTitle) return;

  song.title = clean;
  await saveData();
  if (playlistId && playlistId.toString().startsWith('PL')) renderCards(state.lastSearchResults);
  else await selectPlaylist(playlistId);
  showToast('Titulo actualizado');
}

async function deleteSongAtIndex(playlistId, index) {
  const songs = getSongsForPlaylistId(playlistId);
  const song = songs[index];
  if (!song) return;

  const confirmed = await showConfirm(`Eliminar "${song.title || `Video ${song.id}`}" de la playlist?`);
  if (!confirmed) return;

  songs.splice(index, 1);
  await saveData();
  if (playlistId && playlistId.toString().startsWith('PL')) {
    renderCards(state.lastSearchResults);
    resultsCount.textContent = `${songs.length} Canciones`;
  } else {
    await selectPlaylist(playlistId);
  }
  showToast('Cancion eliminada');
}

async function repairSingleSongAtIndex(playlistId, index) {
  if (!playlistId) return;

  const existingRepair = state.activeRepairs[playlistId];
  if (existingRepair && existingRepair.status !== 'completed') {
    showToast('Ya hay una reparacion activa para esta playlist', true);
    return;
  }

  if (!existingRepair) {
    state.activeRepairs[playlistId] = {
      total: 1,
      checkedCount: 1,
      brokenIndices: [index],
      repairedIndices: [],
      updatedTitleChanges: [],
      status: 'manual_single',
      playlistId
    };
  }

  try {
    await repairBrokenSong(playlistId, index);
    showToast('Reparacion de cancion finalizada');
  } catch (err) {
    showToast(`Error al reparar: ${err}`, true);
  } finally {
    if (state.activeRepairs[playlistId]?.status === 'manual_single') {
      delete state.activeRepairs[playlistId];
    }

    if (state.currentPlaylistId === playlistId) {
      if (playlistId.toString().startsWith('PL')) renderCards(state.lastSearchResults);
      else await selectPlaylist(playlistId);
    }
    updateReopenRepairButton();
  }
}

async function selectPlaylist(id) {
  state.currentPlaylistId = id;
  renderPlaylists();

  let songs = [];
  let title = "Favoritos Locales";

  if (id === 'local') songs = state.favorites;
  else if (id === 'history') {
    songs = state.history;
    title = "Historial";
  } else if (state.playlists[id]) {
    songs = state.playlists[id].songs;
    title = state.playlists[id].title;
  }

  showScreen('playlist');
  resultsTitle.textContent = title;
  resultsCount.textContent = `${songs.length} Canciones`;
  renderCards(songs);
  renderHeaderActions();
}

function renderCards(songs) {
  cardsGrid.innerHTML = '';
  if (!songs || songs.length === 0) {
    cardsGrid.innerHTML = '<div class="no-results">No hay canciones aquí</div>';
    return;
  }

  songs.forEach((s, idx) => {
    // Blacklist check
    if (isBlacklisted(s)) return;

    const showManageActions = canManageSongActions();

    const card = document.createElement('div');
    card.className = 'card';
    card.innerHTML = `
      <div class="card-thumb">
        ${getThumbUrl(s) ? `<img src="${getThumbUrl(s)}" loading="lazy" />` : '<div class="card-thumb-placeholder">VTM</div>'}
        <div class="card-duration">${formatDuration(s.duration)}</div>
        <div class="card-play-overlay">
          <div class="card-play-btn"><svg viewBox="0 0 24 24"><polygon points="5,3 19,12 5,21" fill="currentColor"/></svg></div>
        </div>
      </div>
      <div class="card-info">
        <p class="card-title">${escapeHTML(s.title)}</p>
        <p class="card-artist">${escapeHTML(s.uploader || s.channel || 'YouTube')}</p>
      </div>
      <div class="card-actions">
        <button class="card-play-now-btn" title="Reproducir ahora"><svg viewBox="0 0 24 24" fill="none"><path d="M6 4v16l13-8-13-8z" fill="currentColor"/></svg></button>
        ${showManageActions ? '<button class="card-repair-btn" title="Reparar cancion"><svg viewBox="0 0 24 24" fill="none"><path d="M14.7 6.3a4 4 0 0 0-4.9 4.9L4 17v3h3l5.8-5.8a4 4 0 0 0 4.9-4.9l-2.3 2.3a1 1 0 0 1-1.4 0l-1.2-1.2a1 1 0 0 1 0-1.4z" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg></button>' : ''}
        ${showManageActions ? '<button class="card-edit-btn" title="Editar titulo"><svg viewBox="0 0 24 24" fill="none"><path d="M4 20h4l10-10-4-4L4 16v4z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><path d="M13 7l4 4" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></button>' : ''}
        ${showManageActions ? '<button class="card-delete-btn" title="Borrar cancion"><svg viewBox="0 0 24 24" fill="none"><path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></button>' : ''}
        <button class="card-add-btn" title="Añadir a la cola"><svg viewBox="0 0 24 24" fill="none"><path d="M12 5v14M5 12h14" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg></button>
      </div>
    `;

    card.onclick = () => playVideo(s);

    const playNowBtn = card.querySelector('.card-play-now-btn');
    if (playNowBtn) {
      playNowBtn.onclick = (e) => {
        e.stopPropagation();
        playSongNow(s);
      };
    }

    const repairBtn = card.querySelector('.card-repair-btn');
    if (repairBtn) {
      repairBtn.onclick = async (e) => {
        e.stopPropagation();
        await repairSingleSongAtIndex(state.currentPlaylistId, idx);
      };
    }

    const editBtn = card.querySelector('.card-edit-btn');
    if (editBtn) {
      editBtn.onclick = async (e) => {
        e.stopPropagation();
        await editSongTitleAtIndex(state.currentPlaylistId, idx);
      };
    }

    const deleteBtn = card.querySelector('.card-delete-btn');
    if (deleteBtn) {
      deleteBtn.onclick = async (e) => {
        e.stopPropagation();
        await deleteSongAtIndex(state.currentPlaylistId, idx);
      };
    }

    card.querySelector('.card-add-btn').onclick = (e) => {
      e.stopPropagation();
      addToQueue(s);
    };
    cardsGrid.appendChild(card);
  });
}

// ===== PLAYBACK CORE =====
async function playVideo(video) {
  if (!video) return;
  state.session.video = video;
  state.session.time = 0;
  state.isPlaying = false;
  state.isLoading = true;
  updatePlayButton();

  // Show in bottom bar immediately
  nowPlayingTitle.textContent = video.title;
  nowPlayingArtist.textContent = video.uploader || video.channel || 'YouTube';
  const thumb = getThumbUrl(video);
  nowPlayingImg.src = thumb;
  nowPlayingImg.style.display = thumb ? 'block' : 'none';

  // Update Now Playing Screen
  npTitle.textContent = video.title;
  npArtist.textContent = video.uploader || video.channel || 'YouTube';
  npArtwork.src = thumb;
  npBackground.style.backgroundImage = `url(${thumb || ''})`;
  updateMediaSessionMetadata(video);

  try {
    const streamUrl = await invoke('get_stream_url', { videoId: video.id });
    if (!streamUrl) throw "No se pudo obtener la URL de streaming";

    audio.src = streamUrl;
    audio.play();
    state.isPlaying = true;
    state.isLoading = false;

    // Add to history if not last one
    if (state.history.length === 0 || state.history[0].id !== video.id) {
      state.history.unshift(video);
      if (state.history.length > 100) state.history.pop();
      saveData();
    }
  } catch (err) {
    console.error('Error playing video:', err);
    await handlePlaybackFailure(video, 'get_stream_url', err);
  }
  schedulePlaybackSessionSave(0);
  updatePlayButton();
  updateQueueUI();
}

function updatePlayButton() {
  if (state.isLoading) {
    playSpinner.style.display = 'block';
    iconPlay.style.display = 'none';
    iconPause.style.display = 'none';
  } else {
    playSpinner.style.display = 'none';
    iconPlay.style.display = state.isPlaying ? 'none' : 'block';
    iconPause.style.display = state.isPlaying ? 'block' : 'none';
  }

  const current = state.queue[state.currentIndex];
  if (current) {
    const isFav = state.favorites.some(f => f.id === current.id);
    heartBtn.classList.toggle('active', isFav);
  }

  if (npPlayBtn) {
    const npIconPlay = npPlayBtn.querySelector('.icon-play');
    const npIconPause = npPlayBtn.querySelector('.icon-pause');
    if (npIconPlay && npIconPause) {
      npIconPlay.style.display = state.isPlaying ? 'none' : 'block';
      npIconPause.style.display = state.isPlaying ? 'block' : 'none';
    }
  }

  updateMediaSessionState();
}

function setVolume(val) {
  const normalized = Math.max(0, Math.min(1, Number(val)));
  const effective = getEffectiveVolume(normalized);

  state.volume = normalized;
  audio.volume = Math.max(0, Math.min(1, effective));

  const pct = normalized * 100;
  if (volumeFill) volumeFill.style.width = `${pct}%`;
  if (volumeThumb) volumeThumb.style.left = `${pct}%`;

  // Storage
  state.settings.volume = normalized;
}

function updateMediaSessionMetadata(video) {
  if (!('mediaSession' in navigator) || !video) return;

  try {
    navigator.mediaSession.metadata = new MediaMetadata({
      title: video.title || 'Sin reproducir',
      artist: video.uploader || video.channel || 'YouTube',
      album: 'VTM',
      artwork: getThumbUrl(video) ? [
        { src: getThumbUrl(video), sizes: '96x96', type: 'image/png' },
        { src: getThumbUrl(video), sizes: '128x128', type: 'image/png' },
        { src: getThumbUrl(video), sizes: '192x192', type: 'image/png' }
      ] : []
    });
  } catch (e) {
    console.warn('No se pudo actualizar Media Session metadata:', e);
  }
}

function updateMediaSessionState() {
  if (!('mediaSession' in navigator)) return;

  try {
    navigator.mediaSession.playbackState = state.isPlaying ? 'playing' : 'paused';
  } catch (e) {
    console.warn('No se pudo actualizar Media Session playbackState:', e);
  }
}

function setupMediaSessionControls() {
  if (!('mediaSession' in navigator)) return;

  const mediaActionHandler = (shortcut, fallbackAction) => {
    if (keybindCaptureState.open) {
      const canonical = shortcut || shortcutForBindableAction(fallbackAction);
      if (canonical) {
        keybindCaptureState.capturedShortcut = canonical;
        setKeybindCaptureText(canonical);
        showToast(`Capturado: ${canonical}`);
      }
      return;
    }
    void executeBoundActionsForShortcut(shortcut, fallbackAction);
  };

  const handlerMap = {
    play: () => mediaActionHandler('MediaPlayPause', 'play-pause'),
    pause: () => mediaActionHandler('MediaPlayPause', 'play-pause'),
    stop: () => mediaActionHandler('MediaPlayPause', 'play-pause'),
    previoustrack: () => mediaActionHandler('MediaTrackPrevious', 'previous-song'),
    nexttrack: () => mediaActionHandler('MediaTrackNext', 'next-song'),
    seekbackward: () => {
      const step = 10;
      audio.currentTime = Math.max(0, (audio.currentTime || 0) - step);
    },
    seekforward: () => {
      const step = 10;
      const duration = Number.isFinite(audio.duration) ? audio.duration : Infinity;
      audio.currentTime = Math.min(duration, (audio.currentTime || 0) + step);
    }
  };

  Object.entries(handlerMap).forEach(([action, handler]) => {
    try {
      navigator.mediaSession.setActionHandler(action, handler);
    } catch (e) {
      console.warn(`No se pudo registrar Media Session action ${action}:`, e);
    }
  });

  updateMediaSessionState();
}

// ===== QUEUE & PERSISTENCE =====
function addToQueue(video, options = {}) {
  const {
    allowDuplicate = false,
    showDuplicateToast = true,
    showAddedToast = true
  } = options;

  if (!video || !video.id) return -1;

  const existingIndex = state.queue.findIndex(v => v.id === video.id);
  if (!allowDuplicate && existingIndex !== -1) {
    if (showDuplicateToast) showToast('Esa canción ya está en la cola');
    return existingIndex;
  }

  state.queue.push(video);
  updateQueueUI();
  schedulePlaybackSessionSave(0);
  if (showAddedToast) showToast(`Añadido a la cola: ${video.title}`);
  return state.queue.length - 1;
}

function updateQueueUI() {
  const list = queueList;
  list.innerHTML = '';
  const queueCountEl = document.getElementById('queueCount');
  if (queueCountEl) {
    const count = state.settings.isAutoQueue ? getUpcomingQueueCount() : state.queue.length;
    queueCountEl.textContent = String(count);
  }
  updateAutoReferenceStatus();

  if (state.queue.length === 0) {
    list.innerHTML = '<div class="queue-empty"><p>La cola está vacía</p></div>';
    return;
  }

  state.queue.forEach((s, idx) => {
    const item = document.createElement('div');
    item.className = `queue-item ${idx === state.currentIndex ? 'active' : ''}`;
    item.innerHTML = `
      <span class="queue-item-num">${idx + 1}</span>
      <div class="queue-item-thumb"><img src="${getThumbUrl(s)}" /></div>
      <div class="queue-item-info">
        <p class="queue-item-title">${escapeHTML(s.title)}</p>
        <p class="queue-item-artist">${escapeHTML(s.uploader || 'YouTube')}</p>
      </div>
      <button class="queue-item-remove"><svg viewBox="0 0 24 24" fill="none"><path d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></button>
    `;
    item.onclick = (e) => {
      if (e.target.closest('.queue-item-remove')) {
        state.queue.splice(idx, 1);
        if (state.currentIndex === idx) state.currentIndex = -1;
        else if (state.currentIndex > idx) state.currentIndex--;
        updateQueueUI();
        return;
      }
      playFromQueue(idx, { promoteToFront: true, removeCurrentWhenNoLoop: true });
    };
    list.appendChild(item);
  });
}

function playFromQueue(index, options = {}) {
  const {
    promoteToFront = false,
    removeCurrentWhenNoLoop = false
  } = options;

  if (index < 0 || index >= state.queue.length) return;
  if (promoteToFront) {
    const currentIdx = state.currentIndex;
    const shouldDropCurrent = removeCurrentWhenNoLoop && Number(state.settings.repeatMode || 0) === 0;
    const nextQueue = [];

    nextQueue.push(state.queue[index]);

    for (let i = 0; i < state.queue.length; i += 1) {
      if (i === index) continue;
      if (shouldDropCurrent && i === currentIdx) continue;
      nextQueue.push(state.queue[i]);
    }

    state.queue = nextQueue;
    state.currentIndex = 0;
    updateQueueUI();
  } else {
    state.currentIndex = index;
    const trimmed = trimConsumedQueueForAuto();
    if (trimmed) {
      updateQueueUI();
    }
  }

  playVideo(state.queue[state.currentIndex]);
}

function playNext() {
  if (state.queue.length === 0) return;
  if (state.settings.repeatMode === 2) { // Repeat one
    playFromQueue(state.currentIndex);
    return;
  }
  if (state.settings.isShuffle && state.queue.length > 1) {
    const currentIdx = state.currentIndex >= 0 ? state.currentIndex : 0;
    const candidates = [];
    for (let i = 0; i < state.queue.length; i += 1) {
      if (i !== currentIdx) candidates.push(i);
    }
    if (candidates.length === 0) return;

    const randomIdx = candidates[Math.floor(Math.random() * candidates.length)];
    playFromQueue(randomIdx, { promoteToFront: true, removeCurrentWhenNoLoop: true });
    return;
  }

  let next = state.currentIndex + 1;
  if (next >= state.queue.length) {
    if (state.settings.repeatMode === 1) next = 0; // Repeat all
    else return;
  }
  playFromQueue(next);
}

function playPrev() {
  if (state.queue.length === 0) return;
  if (state.settings.repeatMode === 2) { // Repeat one
    playFromQueue(state.currentIndex);
    return;
  }

  if (state.settings.isShuffle && state.queue.length > 1) {
    const currentIdx = state.currentIndex >= 0 ? state.currentIndex : 0;
    const candidates = [];
    for (let i = 0; i < state.queue.length; i += 1) {
      if (i !== currentIdx) candidates.push(i);
    }
    if (candidates.length === 0) return;

    const randomIdx = candidates[Math.floor(Math.random() * candidates.length)];
    playFromQueue(randomIdx, { promoteToFront: true, removeCurrentWhenNoLoop: true });
    return;
  }

  let prev = state.currentIndex - 1;
  if (prev < 0) {
    if (state.settings.repeatMode === 1) prev = state.queue.length - 1;
    else return;
  }
  playFromQueue(prev, { promoteToFront: true, removeCurrentWhenNoLoop: true });
}

async function saveData() {
  try {
    await invoke('save_data', { filename: 'settings.json', data: state.settings });
    if (state.settings.persistState) {
      await invoke('save_data', { filename: 'playlists.json', data: { playlists: state.playlists } });
      await invoke('save_data', { filename: 'favorites.json', data: { favorites: state.favorites } });
      await invoke('save_data', { filename: 'history.json', data: { history: state.history } });
    }
  } catch (err) {
    console.error('Error saving data:', err);
    const now = Date.now();
    if (now - lastSaveErrorToastAt > 8000) {
      lastSaveErrorToastAt = now;
      showToast('Guardado bloqueado por seguridad. No se sobrescribieron datos valiosos.', true);
    }
  }
}


// ===== PLAYLIST REPAIR =====
async function startPlaylistRepair(playlistId) {
  // Singleton pattern: if already repairing this ID, just show the modal and return
  if (state.activeRepairs[playlistId]) {
    const repair = state.activeRepairs[playlistId];
    if (repair.status === 'completed') {
      delete state.activeRepairs[playlistId];
    } else {
      repairModal.style.display = 'flex';
      recoveryArea.style.display = 'none';

      // Update UI with current progress
      const pct = (repair.checkedCount / repair.total) * 100;
      repairProgressFill.style.width = `${pct}%`;
      repairStatus.textContent = `Verificando (${repair.checkedCount}/${repair.total})...`;

      // If it already finished checking, show results
      if (repair.status === 'checking_done') {
        showRepairResults(playlistId);
      }
      updateReopenRepairButton();
      return;
    }
  }

  // Open immediately so the user sees activity on large playlists/import recovery.
  repairModal.style.display = 'flex';
  recoveryArea.style.display = 'none';
  if (repairRetryButtons) repairRetryButtons.style.display = 'none';
  repairProgressFill.style.width = '0%';
  if (repairRenamedSummary) repairRenamedSummary.style.display = 'none';
  if (repairBrokenSummary) repairBrokenSummary.style.display = 'none';
  repairStatus.textContent = 'Iniciando reparación...\nPreparando playlist...';
  updateReopenRepairButton();

  let songs = [];
  if (playlistId === 'local') songs = state.favorites;
  else if (state.playlists[playlistId]) songs = state.playlists[playlistId].songs;
  else if (playlistId && playlistId.toString().startsWith('PL')) songs = state.lastSearchResults;

  if (songs.length === 0 && playlistId && state.playlists[playlistId]) {
    repairStatus.textContent = 'Recuperando playlist desde Wayback...\nEsto puede tardar en listas grandes.';
    showToast('Intentando recuperar playlist...', false);
    
    try {
      // Try recover_playlist_entries (Wayback snapshots)
      const sourceUrl = state.playlists[playlistId]?.sourceUrl || null;
      const recoveredVideos = await invoke('recover_playlist_entries', { playlistId, sourceUrl });
      
      if (recoveredVideos && recoveredVideos.length > 0) {
        const entries = recoveredVideos.map(video => ({
          id: video.id,
          title: video.title || `Video ${video.id}`,
          duration: video.duration,
          thumbnail: video.thumbnail,
          uploader: video.uploader,
          available: true
        }));
        
        state.playlists[playlistId] = {
          ...state.playlists[playlistId],
          songs: entries
        };
        await saveData();
        songs = entries;
        
        showToast(`Playlist recuperada con ${songs.length} canciones`);
        if (state.currentPlaylistId === playlistId) {
          await selectPlaylist(playlistId);
        }
      }
    } catch (err) {
      const errText = String(err || '');
      console.warn('Recover playlist entries failed:', errText);
      if (errText.includes('wayback_no_snapshots')) {
        showToast('Wayback no tiene snapshots de esa playlist.', true);
      } else if (errText.includes('wayback_no_video_ids')) {
        showToast('Wayback tiene snapshots, pero no se pudieron extraer IDs.', true);
      } else if (errText.includes('wayback_cdx_http_')) {
        showToast('Wayback devolvió un error HTTP al consultar snapshots.', true);
      } else if (errText.includes('wayback_cdx_parse_error')) {
        showToast('No se pudo parsear la respuesta de Wayback.', true);
      }
    }
  }

  if (songs.length === 0) {
    repairStatus.textContent = 'La playlist esta vacia y no se pudieron recuperar canciones.';
    showToast('La playlist está vacía y no se pudieron recuperar canciones', true);
    updateReopenRepairButton();
    return;
  }

  // Initialize background state
  state.activeRepairs[playlistId] = {
    total: songs.length,
    checkedCount: 0,
    brokenIndices: [],
    repairedIndices: [],
    updatedTitleChanges: [],
    titlesResolved: false,
    status: 'checking',
    playlistId: playlistId
  };

  const repair = state.activeRepairs[playlistId];
  repairStatus.textContent = 'Verificando y actualizando titulos...';

  const CONCURRENCY = state.settings.repairThreads || 4;
  const queue = [...songs.keys()];
  let updatedTitles = 0;

  async function worker() {
    while (queue.length > 0) {
      const i = queue.shift();
      const song = songs[i];

      try {
        const check = await invoke('check_video_availability', { videoId: song.id });
        if (!check.available) {
          repair.brokenIndices.push(i);
          const brokenTitle = normalizeRecoveredTitle(song.title || ('Video ' + song.id));
          appendSessionLog(`[REPAIR][BROKEN] playlist=${playlistId} id=${song.id} title="${brokenTitle}"`);
        } else {
          // Check for redirects or title changes
          const oldTitle = song.title || `Video ${song.id}`;
          const currentYtTitle = normalizeRecoveredTitle(check.title || '');
          const archivalTitle = await recoverTitleForVideoId(song.id, oldTitle);
          const normalizedArchival = normalizeRecoveredTitle(archivalTitle);
          const normalizedCurrentLocal = normalizeRecoveredTitle(song.title || '');

          let type = 'none';
          let suggestion = '';

          // 1. Check for ID redirect (definite mismatch)
          if (check.id && check.id !== song.id) {
            type = 'redirect';
            suggestion = currentYtTitle || `Video ${check.id}`;
          } 
          // 2. Check for title mismatch (potential reuse)
          else if (isValidRecoveredTitle(normalizedArchival) && normalizedArchival !== normalizedCurrentLocal) {
            type = 'update';
            suggestion = normalizedArchival;
          }
          // 3. Current title on YT is different from what we thought was the "truth"
          else if (isValidRecoveredTitle(currentYtTitle) && currentYtTitle !== normalizedCurrentLocal) {
            type = 'mismatch';
            suggestion = currentYtTitle;
          }

          if (type !== 'none' && suggestion && suggestion !== normalizedCurrentLocal) {
            updatedTitles++;
            repair.updatedTitleChanges.push({
              index: i,
              id: song.id,
              oldTitle,
              newTitle: suggestion,
              type: type, // 'redirect' | 'update' | 'mismatch'
              status: 'pending' // 'pending' | 'applied' | 'kept' | 'marked_broken'
            });
            appendSessionLog(`[REPAIR][TITLE_SUGGESTION] playlist=${playlistId} id=${song.id} type=${type} old="${oldTitle}" new="${suggestion}"`);
          }

          // If available but missing thumbnail, fix it automatically.
          if (!song.thumbnail || song.thumbnail === '') {
            song.thumbnail = `https://i.ytimg.com/vi/${song.id}/mqdefault.jpg`;
          }
        }
      } catch (e) {
        console.error('Check availability error:', e);
      }

      repair.checkedCount++;
      // Update UI only if the modal is for THIS playlist
      if (repairModal.style.display !== 'none' && state.currentPlaylistId === playlistId) {
        const cleanProgressTitle = normalizeRecoveredTitle(song.title || `Video ${song.id}`) || `Video ${song.id}`;
        repairStatus.textContent = `Verificando y actualizando titulos (${repair.checkedCount}/${repair.total}):\n${cleanProgressTitle}`;
        repairProgressFill.style.width = `${(repair.checkedCount / repair.total) * 100}%`;
      }
      updateReopenRepairButton();
    }
  }

  // Start workers
  const workers = Array.from({ length: Math.min(CONCURRENCY, songs.length) }, () => worker());
  await Promise.all(workers);

  if (updatedTitles > 0) {
    await saveData();
    renderRepairRenamedSummary(playlistId, repair);
    showToast(`Titulos actualizados: ${updatedTitles}`);
  }

  repair.status = 'checking_done';
  if (repairModal.style.display !== 'none' && state.currentPlaylistId === playlistId) {
    showRepairResults(playlistId);
  } else {
    showToast(`Verificación de playlist completada: ${repair.brokenIndices.length} caídas.`);
  }
  updateReopenRepairButton();
}

function showRepairResults(playlistId) {
  const repair = state.activeRepairs[playlistId];
  if (!repair) return;
  renderRepairRenamedSummary(playlistId, repair);
  renderRepairBrokenSummary(playlistId, repair);
  if (recoveryArea) recoveryArea.style.display = 'none';
  if (repairRetryButtons) repairRetryButtons.style.display = 'none';

  if (repair.brokenIndices.length === 0) {
    const renamedCount = Array.isArray(repair.updatedTitleChanges) ? repair.updatedTitleChanges.length : 0;
    if (renamedCount > 0) {
      repairStatus.textContent = `No hay canciones caídas. Se actualizaron ${renamedCount} titulos.`;
      repair.status = 'completed';

      if (state.currentPlaylistId === playlistId) {
        if (playlistId.toString().startsWith('PL')) renderCards(state.lastSearchResults);
        else selectPlaylist(playlistId);
      }
      updateReopenRepairButton();
      return;
    }

    repairStatus.textContent = '¡Todo en orden! No se encontraron canciones caídas.';
    repair.status = 'completed';
    setTimeout(() => {
      if (repairModal.style.display !== 'none') repairModal.style.display = 'none';

      // Render updated cards without deleting state
      if (state.currentPlaylistId === playlistId) {
        if (playlistId.toString().startsWith('PL')) renderCards(state.lastSearchResults);
        else selectPlaylist(playlistId);
      }
      updateReopenRepairButton();
    }, 2000);
    return;
  }

  const renamedCount = Array.isArray(repair.updatedTitleChanges) ? repair.updatedTitleChanges.length : 0;
  repairStatus.textContent = `Analisis completado. Títulos actualizados: ${renamedCount}. Canciones caídas: ${repair.brokenIndices.length}.`;
}

function renderRepairRenamedSummary(playlistId, repair) {
  if (!repairRenamedSummary || !repairRenamedCount || !openRenamedListBtn) return;

  const changes = Array.isArray(repair?.updatedTitleChanges) ? repair.updatedTitleChanges : [];
  const pending = changes.filter(c => c.status === 'pending');
  const count = pending.length;
  
  if (count === 0) {
    repairRenamedSummary.style.display = 'none';
    return;
  }

  repairRenamedSummary.style.display = 'block';
  repairRenamedCount.textContent = `${count} títulos no coinciden`;
  
  const openAction = () => {
    openRenamedTitlesModal(playlistId, pending);
  };

  openRenamedListBtn.onclick = openAction;
  repairRenamedSummary.onclick = openAction;
  repairRenamedSummary.style.cursor = 'pointer';
  
  if (!repair.titlesResolved) {
    repairRenamedSummary.classList.add('needs-action');
  } else {
    repairRenamedSummary.classList.remove('needs-action');
  }
}

function renderRepairBrokenSummary(playlistId, repair) {
  if (!repairBrokenSummary || !repairBrokenCount || !startBrokenRepairBtn) return;

  const brokenCount = Array.isArray(repair?.brokenIndices) ? repair.brokenIndices.length : 0;
  if (brokenCount <= 0) {
    repairBrokenSummary.style.display = 'none';
    startBrokenRepairBtn.onclick = null;
    return;
  }

  const pendingTitles = Array.isArray(repair?.updatedTitleChanges) ? repair.updatedTitleChanges.filter(c => c.status === 'pending').length : 0;
  const isBlocked = pendingTitles > 0 && !repair.titlesResolved;

  repairBrokenCount.textContent = `${brokenCount} canciones caidas detectadas`;
  startBrokenRepairBtn.textContent = isBlocked ? 'Revisa primero los títulos' : 'Gestionar manualmente';
  startBrokenRepairBtn.disabled = isBlocked;
  
  if (isBlocked) {
    startBrokenRepairBtn.title = 'Debes revisar y confirmar los títulos no coincidentes antes de continuar.';
  } else {
    startBrokenRepairBtn.title = 'Abrir interfaz interactiva para solucionar canciones caídas.';
    startBrokenRepairBtn.onclick = async () => {
      startBrokenRepairBtn.disabled = true;
      startBrokenRepairBtn.textContent = 'Preparando...';
      try {
        const started = await startRecoveryProcess(playlistId);
        if (started === false) {
          showToast('La preparación ya está en curso. Espera a que termine.', true);
        }
      } catch (e) {
        console.error('Error starting manual repair preparation:', e);
        showToast('No se pudo iniciar la preparación manual', true);
      } finally {
        const current = state.activeRepairs[playlistId];
        if (!current || current.status !== 'recovering') {
          startBrokenRepairBtn.disabled = false;
          startBrokenRepairBtn.textContent = 'Gestionar manualmente';
        }
      }
    };
  }
  repairBrokenSummary.style.display = 'flex';
}

function openRenamedTitlesModal(playlistId, pending) {
  const repair = state.activeRepairs[playlistId];
  if (!repair) return;

  const safeChanges = Array.isArray(pending) ? pending : [];
  const count = safeChanges.length;
  renamedTitlesMeta.textContent = `${count} títulos no coinciden`;

  if (count === 0) {
    renamedTitlesList.innerHTML = '<div class="renamed-titles-empty">No hay cambios para mostrar.</div>';
  } else {
    renamedTitlesList.innerHTML = `
      <div class="renamed-titles-scrollable">
        ${safeChanges.map((item) => {
          const absoluteIdx = repair.updatedTitleChanges.indexOf(item);
          const oldTitle = escapeHTML(item.oldTitle || `Video ${item.id}`);
          const newTitle = escapeHTML(item.newTitle || 'Unknown');
          
          return `
            <div class="renamed-title-list-item" data-idx="${absoluteIdx}">
              <div class="renamed-title-row-meta">
                <span>Song ID: ${escapeHTML(item.id)}</span>
                <span>Índice: ${item.index}</span>
              </div>
              <div class="renamed-title-body">
                <div class="renamed-actions-group">
                <label class="renamed-action-btn action-keep" title="Mantener el título que ya tienes">
                  <input type="radio" name="action-${absoluteIdx}" value="mantener" checked>
                  <span class="action-text">mantener</span>
                </label>
                
                <label class="renamed-action-btn action-change" title="Cambiar al título detectado en YouTube">
                  <input type="radio" name="action-${absoluteIdx}" value="cambiar">
                  <span class="action-text">cambiar</span>
                </label>

                <label class="renamed-action-btn action-broken" title="Marcar esta canción como caída/no disponible">
                  <input type="radio" name="action-${absoluteIdx}" value="caida">
                  <span class="action-text">caida</span>
                </label>
                </div>

                <div class="renamed-title-diff-card">
                  <div class="diff-old-title">${oldTitle}</div>
                  <div class="diff-arrow-down">&darr;</div>
                  <div class="diff-new-title">${newTitle}</div>
                </div>
              </div>
            </div>
          `;
        }).join('')}
      </div>
      <div class="renamed-titles-footer">
        <button id="confirmTitlesBtn" class="btn-primary" style="width:100%; padding: 12px;">Confirmar y continuar</button>
      </div>
    `;

    const confirmBtn = document.getElementById('confirmTitlesBtn');
    confirmBtn.onclick = () => confirmTitleResolutions(playlistId);
  }

  renamedTitlesModal.style.display = 'flex';
}

async function confirmTitleResolutions(playlistId) {
  const repair = state.activeRepairs[playlistId];
  if (!repair) return;

  let songs = [];
  if (playlistId === 'local') songs = state.favorites;
  else if (state.playlists[playlistId]) songs = state.playlists[playlistId].songs;
  else if (playlistId && playlistId.toString().startsWith('PL')) songs = state.lastSearchResults;

  const rows = renamedTitlesList.querySelectorAll('.renamed-title-list-item');
  let appliedCount = 0;
  let keptCount = 0;
  let markedBrokenCount = 0;

  rows.forEach(row => {
    const idx = parseInt(row.dataset.idx);
    const selectedAction = row.querySelector('input[type="radio"]:checked')?.value;
    const change = repair.updatedTitleChanges[idx];
    
    if (change && change.status === 'pending') {
      if (selectedAction === 'cambiar') {
        const song = songs[change.index];
        if (song) {
          song.title = change.newTitle;
          change.status = 'applied';
          appliedCount++;
        }
      } else if (selectedAction === 'caida') {
        change.status = 'marked_broken';
        if (!repair.brokenIndices.includes(change.index)) {
          repair.brokenIndices.push(change.index);
        }
        markedBrokenCount++;
      } else {
        change.status = 'kept';
        keptCount++;
      }
    }
  });

  repair.titlesResolved = true;
  renamedTitlesModal.style.display = 'none';

  const resolutionSummary = `Titulos alterados: ${appliedCount} | Titulos conservados: ${keptCount} | Canciones desechadas: ${markedBrokenCount}`;
  showToast(resolutionSummary);

  if (repairModal.style.display !== 'none' && state.currentPlaylistId === playlistId) {
    repairStatus.textContent = `Resolución de títulos completada.\nTitulos alterados: ${appliedCount}\nTitulos conservados: ${keptCount}\nCanciones desechadas: ${markedBrokenCount}`;
  }

  await saveData();
  renderRepairRenamedSummary(playlistId, repair);
  renderRepairBrokenSummary(playlistId, repair);

  if (state.currentPlaylistId === playlistId) {
    if (playlistId.toString().startsWith('PL')) renderCards(state.lastSearchResults);
    else selectPlaylist(playlistId);
  }
}

async function applyRenamedTitlesAction(playlistId, changeIdx, action) {
  const repair = state.activeRepairs[playlistId];
  if (!repair) return;

  const change = repair.updatedTitleChanges[changeIdx];
  if (!change || change.status !== 'pending') return;

  let songs = [];
  if (playlistId === 'local') songs = state.favorites;
  else if (state.playlists[playlistId]) songs = state.playlists[playlistId].songs;
  else if (playlistId && playlistId.toString().startsWith('PL')) songs = state.lastSearchResults;

  const song = songs[change.index];
  if (!song) return;

  if (action === 'apply') {
    song.title = change.newTitle;
    change.status = 'applied';
    showToast('Título actualizado');
  } else if (action === 'keep') {
    change.status = 'kept';
    showToast('Título original mantenido');
  } else if (action === 'break') {
    change.status = 'marked_broken';
    if (!repair.brokenIndices.includes(change.index)) {
      repair.brokenIndices.push(change.index);
    }
    showToast('Marcada como caída');
  }

  // Update lists
  const pending = repair.updatedTitleChanges.filter(c => c.status === 'pending');
  if (pending.length === 0) {
    renamedTitlesModal.style.display = 'none';
  } else {
    openRenamedTitlesModal(pending);
  }

  renderRepairRenamedSummary(repair);
  renderRepairBrokenSummary(playlistId, repair);
  await saveData();
  
  if (state.currentPlaylistId === playlistId) {
    if (playlistId.toString().startsWith('PL')) renderCards(state.lastSearchResults);
    else selectPlaylist(playlistId);
  }
}

async function applyAllRenamedTitles(playlistId) {
  const repair = state.activeRepairs[playlistId];
  if (!repair) return;

  const pending = repair.updatedTitleChanges.filter(c => c.status === 'pending');
  if (pending.length === 0) return;

  const confirmed = await showConfirm(`¿Actualizar los títulos de ${pending.length} canciones detectadas?`);
  if (!confirmed) return;

  let songs = [];
  if (playlistId === 'local') songs = state.favorites;
  else if (state.playlists[playlistId]) songs = state.playlists[playlistId].songs;
  else if (playlistId && playlistId.toString().startsWith('PL')) songs = state.lastSearchResults;

  let count = 0;
  pending.forEach(change => {
    const song = songs[change.index];
    if (song) {
      song.title = change.newTitle;
      change.status = 'applied';
      count++;
    }
  });

  renamedTitlesModal.style.display = 'none';
  renderRepairRenamedSummary(repair);
  await saveData();
  
  if (state.currentPlaylistId === playlistId) {
    if (playlistId.toString().startsWith('PL')) renderCards(state.lastSearchResults);
    else selectPlaylist(playlistId);
  }
  showToast(`${count} títulos actualizados.`);
}

async function autoRepairBrokenSong(playlistId, index, song, repairState) {
  if (!song || !song.id) return false;

  let recoveredTitle = '';
  try {
    const candidate = normalizeRecoveredTitle(await recoverTitleForVideoId(song.id, song.title));
    if (isValidRecoveredTitle(candidate)) {
      recoveredTitle = candidate;
    }
  } catch (e) {
    await appendSessionLog(`[REPAIR][AUTO_TITLE_FAIL] playlist=${playlistId} id=${song.id} error=${stringifyError(e)}`);
  }

  if (recoveredTitle) {
    const oldTitle = normalizeRecoveredTitle(song.title || `Video ${song.id}`) || `Video ${song.id}`;
    if (oldTitle !== recoveredTitle) {
      song.title = recoveredTitle;
      if (repairState) {
        if (!Array.isArray(repairState.updatedTitleChanges)) {
          repairState.updatedTitleChanges = [];
        }
        const alreadyTracked = repairState.updatedTitleChanges.some(
          (c) => c && c.id === song.id && c.newTitle === recoveredTitle
        );
        if (!alreadyTracked) {
          repairState.updatedTitleChanges.push({
            id: song.id,
            oldTitle,
            newTitle: recoveredTitle
          });
        }
      }
    }
  }

  const query = recoveredTitle || normalizeRecoveredTitle(song.title || '');
  if (!query) {
    await appendSessionLog(`[REPAIR][AUTO_SKIP] playlist=${playlistId} id=${song.id} reason=no_query_title`);
    return false;
  }

  let candidates = [];
  try {
    const searchResults = await invoke('search_youtube', { query });
    candidates = Array.isArray(searchResults) ? searchResults.slice(0, 12) : [];
  } catch (e) {
    await appendSessionLog(`[REPAIR][AUTO_SEARCH_FAIL] playlist=${playlistId} id=${song.id} query="${query}" error=${stringifyError(e)}`);
    return false;
  }

  if (candidates.length === 0) {
    await appendSessionLog(`[REPAIR][AUTO_SKIP] playlist=${playlistId} id=${song.id} query="${query}" reason=no_candidates`);
    return false;
  }

  let chosen = candidates.find((c) => c && c.id && c.id !== song.id);
  if (!chosen) {
    chosen = candidates.find((c) => c && c.id);
  }
  if (!chosen || !chosen.id) {
    await appendSessionLog(`[REPAIR][AUTO_SKIP] playlist=${playlistId} id=${song.id} query="${query}" reason=invalid_candidate`);
    return false;
  }

  const oldId = song.id;
  const oldTitle = normalizeRecoveredTitle(song.title || `Video ${oldId}`) || `Video ${oldId}`;

  song.id = chosen.id;
  song.title = normalizeRecoveredTitle(chosen.title || song.title || recoveredTitle || `Video ${chosen.id}`);
  song.thumbnail = chosen.thumbnail || `https://i.ytimg.com/vi/${chosen.id}/mqdefault.jpg`;
  song.uploader = chosen.uploader || song.uploader;
  if (chosen.duration !== undefined) song.duration = chosen.duration;

  if (repairState) {
    if (!Array.isArray(repairState.repairedIndices)) {
      repairState.repairedIndices = [];
    }
    if (!repairState.repairedIndices.includes(index)) {
      repairState.repairedIndices.push(index);
    }

    if (!Array.isArray(repairState.updatedTitleChanges)) {
      repairState.updatedTitleChanges = [];
    }
    const cleanNewTitle = normalizeRecoveredTitle(song.title || `Video ${song.id}`) || `Video ${song.id}`;
    if (cleanNewTitle !== oldTitle) {
      const alreadyTracked = repairState.updatedTitleChanges.some(
        (c) => c && c.id === song.id && c.newTitle === cleanNewTitle
      );
      if (!alreadyTracked) {
        repairState.updatedTitleChanges.push({
          id: song.id,
          oldTitle,
          newTitle: cleanNewTitle
        });
      }
    }
  }

  await appendSessionLog(`[REPAIR][AUTO_FIXED] playlist=${playlistId} old_id=${oldId} new_id=${song.id} title="${normalizeRecoveredTitle(song.title || '')}"`);
  return true;
}

async function startRecoveryProcess(playlistId) {
  const repair = state.activeRepairs[playlistId];
  if (!repair) return false;

  if (repair.status === 'recovering') return false;
  repair.status = 'recovering';

  if (repairBrokenSummary) repairBrokenSummary.style.display = 'none';
  if (repairRetryButtons) repairRetryButtons.style.display = 'none';
  if (recoveryArea) recoveryArea.style.display = 'none';

  let songs = [];
  if (playlistId === 'local') songs = state.favorites;
  else if (state.playlists[playlistId]) songs = state.playlists[playlistId].songs;
  else if (playlistId && playlistId.toString().startsWith('PL')) songs = state.lastSearchResults;

  if (repairLog) repairLog.textContent = '';

  const indices = [...repair.brokenIndices];
  const total = indices.length;
  if (total === 0) {
    repair.status = 'completed';
    repairStatus.textContent = 'Reparación completada';
    updateReopenRepairButton();
    return true;
  }

  let processed = 0;
  let updatedBrokenTitles = 0;
  repairStatus.textContent = `Iniciando preparación y reparación (${processed}/${total})...`;

  try {
    const brokenList = [...indices];
    let step = 0;
    for (const idx of brokenList) {
      step += 1;

      const currentRepair = state.activeRepairs[playlistId];
      if (!currentRepair || !Array.isArray(currentRepair.brokenIndices) || !currentRepair.brokenIndices.includes(idx)) {
        continue;
      }

      const song = songs[idx];
      if (!song) {
        processed += 1;
        continue;
      }

      if (repairModal.style.display !== 'none' && state.currentPlaylistId === playlistId) {
        repairStatus.textContent = `Preparando ${step}/${brokenList.length}...`;
      }

      try {
        const currentTitle = normalizeRecoveredTitle(song.title || `Video ${song.id}`) || `Video ${song.id}`;
        if (isValidRecoveredTitle(currentTitle)) {
          const recoveredTitle = normalizeRecoveredTitle(await recoverTitleForVideoId(song.id, currentTitle));

          if (isValidRecoveredTitle(recoveredTitle) && recoveredTitle !== currentTitle) {
            song.title = recoveredTitle;
            updatedBrokenTitles += 1;

            if (!Array.isArray(repair.updatedTitleChanges)) {
              repair.updatedTitleChanges = [];
            }

            const alreadyTracked = repair.updatedTitleChanges.some(
              (c) => c && c.id === song.id && c.newTitle === recoveredTitle
            );

            if (!alreadyTracked) {
              repair.updatedTitleChanges.push({
                index: idx,
                id: song.id,
                oldTitle: currentTitle,
                newTitle: recoveredTitle,
                type: 'update',
                status: 'applied'
              });
            }

            await appendSessionLog(`[REPAIR][MANUAL_PREP_TITLE] playlist=${playlistId} id=${song.id} old="${currentTitle}" new="${recoveredTitle}"`);
          }
        } else {
          await appendSessionLog(`[REPAIR][MANUAL_PREP_SKIP] playlist=${playlistId} id=${song.id} reason=invalid_initial_title`);
        }
      } catch (e) {
        console.error('Error preparing manual repair song:', e);
        await appendSessionLog(`[REPAIR][MANUAL_PREP_ERROR] playlist=${playlistId} id=${song.id} error=${stringifyError(e)}`);
      }

      processed += 1;
      if (repairModal.style.display !== 'none' && state.currentPlaylistId === playlistId) {
        const improvedTitlesStatus = updatedBrokenTitles > 0
          ? `\nTitulos mejorados: ${updatedBrokenTitles}`
          : '';
        repairStatus.textContent = `Preparado ${processed}/${total}. Abriendo revisión manual...${improvedTitlesStatus}`;
      }
      updateReopenRepairButton();

      await saveData();
      renderPlaylists();

      repair.status = 'checking_done';
      repairStatus.textContent = `Revisión manual ${processed}/${total}: esperando tu acción para continuar...`;
      await repairBrokenSong(playlistId, idx);
    }

    const remainingBroken = Array.isArray(repair.brokenIndices) ? repair.brokenIndices.length : 0;
    
    if (remainingBroken > 0) {
      repairStatus.textContent = `Quedan ${remainingBroken} canciones caídas sin resolver. Puedes continuar manualmente.`;

      // FINAL CHECK: If no more broken indices, close or update status
      const finalRepair = state.activeRepairs[playlistId];
      if (!finalRepair || !finalRepair.brokenIndices || finalRepair.brokenIndices.length === 0) {
        repairStatus.textContent = '✓ Todas las canciones han sido procesadas.';
        setTimeout(() => {
          if (repairModal.style.display !== 'none') {
            repairModal.style.display = 'none';
            if (renamedTitlesModal) renamedTitlesModal.style.display = 'none';
            showToast('Reparación finalizada con éxito');
          }
        }, 2000);
      }
    } else {
      repairStatus.textContent = '¡Reparación completada! No quedan canciones caídas.';
      setTimeout(() => {
        if (repairModal.style.display !== 'none') repairModal.style.display = 'none';
      }, 1500);
    }

    if (state.currentPlaylistId === playlistId) {
      if (playlistId.toString().startsWith('PL')) renderCards(state.lastSearchResults);
      else selectPlaylist(playlistId);
    }

    return true;
  } catch (e) {
    repair.status = 'checking_done';
    console.error('Manual repair preparation failed:', e);
    await appendSessionLog(`[REPAIR][MANUAL_PREP_FATAL] playlist=${playlistId} error=${stringifyError(e)}`);
    repairStatus.textContent = 'Error al preparar la reparación manual.';
    renderRepairBrokenSummary(playlistId, repair);
    return false;
  }
}

async function repairBrokenSong(playlistId, index) {
  const isValidTitle = isValidRecoveredTitle;

  let songs = [];
  if (playlistId === 'local') songs = state.favorites;
  else if (state.playlists[playlistId]) songs = state.playlists[playlistId].songs;
  else if (playlistId && playlistId.toString().startsWith('PL')) songs = state.lastSearchResults;

  if (!songs || !Array.isArray(songs) || index >= songs.length) {
    showToast('Error: No se puede acceder a la canción', true);
    return;
  }
  const song = songs[index];
  const repairState = state.activeRepairs[playlistId];

  const applyRecoveredTitleIfBetter = async (newTitle) => {
    const normalizedNewTitle = normalizeRecoveredTitle(newTitle);
    if (!isValidRecoveredTitle(normalizedNewTitle)) return false;

    const oldTitle = normalizeRecoveredTitle(song.title || `Video ${song.id}`) || `Video ${song.id}`;
    const cleanTitle = normalizedNewTitle;
    if (!cleanTitle || cleanTitle === oldTitle) return false;

    song.title = cleanTitle;

    if (repairState) {
      if (!Array.isArray(repairState.updatedTitleChanges)) {
        repairState.updatedTitleChanges = [];
      }

      const alreadyTracked = repairState.updatedTitleChanges.some(
        (c) => c && c.id === song.id && c.newTitle === cleanTitle
      );

      if (!alreadyTracked) {
        repairState.updatedTitleChanges.push({
          id: song.id,
          oldTitle,
          newTitle: cleanTitle
        });
      }
    }

    await saveData();
    return true;
  };

  const setButtonsDisabled = (disabled) => {
    retryTechRobo.disabled = disabled;
    retryFilmot.disabled = disabled;
    retryWayback.disabled = disabled;
  };

  const retriggerRecovery = async (method) => {
    setButtonsDisabled(true);
    retryTechRobo.classList.remove('active');
    retryFilmot.classList.remove('active');
    retryWayback.classList.remove('active');

    if (method === 'techrobo') retryTechRobo.classList.add('active');
    if (method === 'filmot') retryFilmot.classList.add('active');
    if (method === 'wayback') retryWayback.classList.add('active');

    let currentTitle = '';
    let currentSource = '';

    try {
      if (method === 'techrobo') {
        if (repairLog) repairLog.textContent = 'Reintentando TechRobo...';
        currentTitle = await invoke('recover_from_techrobo', { videoId: song.id });
        currentSource = 'Metadata Archive';
      } else if (method === 'filmot') {
        if (repairLog) repairLog.textContent = 'Reintentando Filmot directo...';
        currentTitle = await invoke('recover_from_filmot', { videoId: song.id });
        currentSource = 'Filmot';
      } else if (method === 'wayback') {
        if (repairLog) repairLog.textContent = 'Reintentando Wayback Machine...';
        currentTitle = await invoke('recover_from_wayback', { videoId: song.id });
        currentSource = 'Wayback Machine';
      }

      if (currentTitle && isValidTitle(currentTitle)) {
        recoveredTitle = currentTitle;
        source = currentSource;
        brokenSongTitleNew.textContent = recoveredTitle;
        if (repairLog) repairLog.textContent = `Título recuperado via ${method.toUpperCase()}.`;

        // Search candidates with the newly found title
        if (repairLog) repairLog.textContent = `Buscando candidatos en YouTube... (${source})`;
        const results = await invoke('search_youtube', { query: recoveredTitle });
        const candidates = results.slice(0, 10);
        if (repairLog) repairLog.textContent = `Selecciona el mejor reemplazo. (${source})`;
        renderCandidates(candidates, (chosen) => {
          updateBrokenSong(playlistId, index, chosen);
          state.onCandidateSelected();
        });
      } else {
        if (repairLog) repairLog.textContent = `No se obtuvo título válido con ${method.toUpperCase()}.`;
      }
    } catch (err) {
      if (repairLog) repairLog.textContent = `Error en ${method.toUpperCase()}: ${err}`;
    } finally {
      setButtonsDisabled(false);
    }
  };

  retryTechRobo.onclick = () => retriggerRecovery('techrobo');
  retryFilmot.onclick = () => retriggerRecovery('filmot');
  retryWayback.onclick = () => retriggerRecovery('wayback');

  repairModal.style.display = 'flex';
  recoveryArea.style.display = 'block';
  repairRetryButtons.style.display = 'flex';
  setButtonsDisabled(true);
  brokenSongTitleOld.textContent = song.title;
  if (repairLog) repairLog.textContent = 'Preparando opciones de recuperación...';

  let recoveredTitle = '';
  let source = 'Ninguna';

  try {
    if (repairLog) repairLog.textContent = 'Buscando título recuperado en paralelo...';
    retryTechRobo.classList.add('active');
    retryFilmot.classList.add('active');
    retryWayback.classList.add('active');

    const title = normalizeRecoveredTitle(await recoverTitleForVideoId(song.id, song.title));
    if (title && isValidTitle(title)) {
      recoveredTitle = title;
      source = normalizeRecoveredTitle(song.title || '') === title ? 'Original' : 'Metadatos recuperados';
      if (repairLog) repairLog.textContent = `Título recuperado: ${source}.`;
    } else if (isValidTitle(song.title)) {
      recoveredTitle = normalizeRecoveredTitle(song.title);
      source = 'Original';
      if (repairLog) repairLog.textContent = 'No se encontró un título mejor. Usando el original.';
    } else {
      recoveredTitle = '';
      source = 'Desconocido';
      if (repairLog) repairLog.textContent = 'No se pudo recuperar el título. Usa reintento o reemplazo manual.';
    }
  } catch (e) {
    console.log('Title recovery failed', e);
    if (repairLog) repairLog.textContent = 'Error recuperando el título. Usa reintento o reemplazo manual.';
  }

  setButtonsDisabled(false);
  recoveredTitle = normalizeRecoveredTitle(recoveredTitle);
  brokenSongTitleNew.textContent = recoveredTitle || 'Título Desconocido';

  if (recoveredTitle) {
    try {
      const titleChanged = await applyRecoveredTitleIfBetter(recoveredTitle);
      if (titleChanged && repairLog) {
        repairLog.textContent = `Título actualizado por ID. Buscando candidatos en YouTube... (${source})`;
      }
    } catch (e) {
      console.warn('No se pudo guardar el título recuperado:', e);
    }
  }

  try {
    if (recoveredTitle) {
      if (repairLog) repairLog.textContent = `Buscando candidatos en YouTube... (${source})`;
      const results = await invoke('search_youtube', { query: recoveredTitle });
      const candidates = results.slice(0, 10);
      if (candidates.length === 0) {
        if (repairLog) repairLog.textContent = `No hubo candidatos para "${recoveredTitle}". Puedes usar URL manual u omitir.`;
        candidatesGrid.innerHTML = '<p style="text-align:center; padding: 20px; color: var(--text-secondary);">No se encontraron candidatos con ese título. Usa URL manual u Omitir.</p>';
      } else {
        if (repairLog) repairLog.textContent = `Selecciona el mejor reemplazo. (${source})`;
        renderCandidates(candidates, (chosen) => {
          updateBrokenSong(playlistId, index, chosen);
          state.onCandidateSelected();
        });
      }
    } else {
      candidatesGrid.innerHTML = '<p style="text-align:center; padding: 20px; color: var(--text-secondary);">Recuperación fallida. Prueba los botones de arriba o manual.</p>';
    }
  } catch (e) {
    candidatesGrid.innerHTML = '<p>Error al buscar candidatos. Introduce un enlace manual.</p>';
  }

  return new Promise((resolve) => {
    const skipBtn = document.getElementById('skipRecoveryBtn');

    const cleanup = () => {
      submitManualUrl.onclick = null;
      skipBtn.onclick = null;
      retryTechRobo.onclick = null;
      retryFilmot.onclick = null;
      retryWayback.onclick = null;
      manualUrlInput.value = '';
      state.onCandidateSelected = null;
    };

    const onManual = async () => {
      const url = manualUrlInput.value.trim();
      if (!url) return;
      try {
        const videoId = extractVideoId(url);
        if (videoId) {
          updateBrokenSong(playlistId, index, { id: videoId, title: recoveredTitle });
          cleanup();
          resolve();
        }
      } catch (e) { showToast('URL no válida', true); }
    };

    const onSkip = () => {
      const repair = state.activeRepairs[playlistId];
      if (repair && Array.isArray(repair.brokenIndices)) {
        repair.brokenIndices = repair.brokenIndices.filter(i => i !== index);
      }
      cleanup();
      resolve();
      
      // Refresh summaries
      if (repair) {
        renderRepairBrokenSummary(playlistId, repair);
        renderRepairRenamedSummary(playlistId, repair);
      }
    };

    submitManualUrl.onclick = onManual;
    skipBtn.onclick = onSkip;

    state.onCandidateSelected = () => {
      cleanup();
      resolve();
    };
  });
}

function renderCandidates(candidates, onSelect) {
  candidatesGrid.innerHTML = candidates.map(c => `
    <div class="candidate-card" data-id="${c.id}">
      <div class="candidate-thumb">
        <img src="${getThumbUrl(c)}" />
        <div class="card-duration">${formatDuration(c.duration)}</div>
        <div class="candidate-preview-btn" title="Previsualizar">
          <div class="preview-icon">
            <svg viewBox="0 0 24 24" fill="none"><polygon points="5,3 19,12 5,21" fill="currentColor"/></svg>
          </div>
        </div>
      </div>
      <div class="candidate-info">
        <p class="candidate-title selectable-text">${escapeHTML(c.title)}</p>
        <p class="candidate-artist">${escapeHTML(c.uploader || 'YouTube')}</p>
      </div>
    </div>
  `).join('');

  candidatesGrid.querySelectorAll('.candidate-card').forEach((card, idx) => {
    const previewBtn = card.querySelector('.candidate-preview-btn');

    previewBtn.onclick = async (e) => {
      e.stopPropagation();
      const video = candidates[idx];
      showToast(`Previsualizando: ${video.title}`);
      try {
        const streamUrl = await invoke('get_stream_url', { videoId: video.id });
        audio.src = streamUrl;
        audio.play();
      } catch (err) {
        showToast('Error al previsualizar', true);
      }
    };

    card.onclick = () => {
      candidatesGrid.innerHTML = '';
      onSelect(candidates[idx]);
      state.onCandidateSelected();
    };
  });
}

function updateBrokenSong(playlistId, index, newSong) {
  if (playlistId === 'local') {
    if (!state.favorites) state.favorites = [];
    if (state.favorites[index]) {
      state.favorites[index] = {
        ...state.favorites[index],
        id: newSong.id,
        title: newSong.title,
        thumbnail: newSong.thumbnail || `https://i.ytimg.com/vi/${newSong.id}/mqdefault.jpg`
      };
    }
  } else if (state.playlists[playlistId] && state.playlists[playlistId].songs) {
    if (state.playlists[playlistId].songs[index]) {
      state.playlists[playlistId].songs[index] = {
        ...state.playlists[playlistId].songs[index],
        id: newSong.id,
        title: newSong.title,
        thumbnail: newSong.thumbnail || `https://i.ytimg.com/vi/${newSong.id}/mqdefault.jpg`
      };
    }
  } else if (playlistId && playlistId.toString().startsWith('PL')) {
    if (!state.lastSearchResults) state.lastSearchResults = [];
    if (state.lastSearchResults[index]) {
      state.lastSearchResults[index] = {
        ...state.lastSearchResults[index],
        id: newSong.id,
        title: newSong.title,
        thumbnail: newSong.thumbnail || `https://i.ytimg.com/vi/${newSong.id}/mqdefault.jpg`
      };
    }
  }

  if (!state.activeRepairs[playlistId]) {
    state.activeRepairs[playlistId] = { brokenIndices: [], repairedIndices: [], status: 'completed', playlistId: playlistId };
  } else if (!state.activeRepairs[playlistId].repairedIndices) {
    state.activeRepairs[playlistId].repairedIndices = [];
  }

  if (!state.activeRepairs[playlistId].repairedIndices.includes(index)) {
    state.activeRepairs[playlistId].repairedIndices.push(index);
  }
  if (state.activeRepairs[playlistId].brokenIndices) {
    state.activeRepairs[playlistId].brokenIndices = state.activeRepairs[playlistId].brokenIndices.filter(i => i !== index);
  }

  saveData();
  renderPlaylists();
  
  const currentRepair = state.activeRepairs[playlistId];
  if (currentRepair) {
    renderRepairBrokenSummary(playlistId, currentRepair);
    renderRepairRenamedSummary(playlistId, currentRepair);
  }

  if (state.currentPlaylistId === playlistId) {
    if (playlistId.toString().startsWith('PL')) renderCards(state.lastSearchResults);
    else selectPlaylist(playlistId);
  }
}

function extractVideoId(url) {
  const regExp = /^.*(youtu.be\/|v\/|u\/\w\/|embed\/|watch\?v=|\&v=)([^#\&\?]*).*/;
  const match = url.match(regExp);
  return (match && match[2].length === 11) ? match[2] : (url.length === 11 ? url : null);
}

function isValidVideoId(id) {
  return typeof id === 'string' && /^[A-Za-z0-9_-]{11}$/.test(id.trim());
}

function getEntryVideoId(entry) {
  if (!entry) return null;

  const direct = typeof entry.id === 'string' ? entry.id.trim() : '';
  if (isValidVideoId(direct)) return direct;

  const candidates = [
    entry.url,
    entry.webpage_url,
    entry.original_url,
    entry.webpage_url_basename,
    entry.display_id
  ];

  for (const candidate of candidates) {
    if (!candidate) continue;

    const raw = String(candidate).trim();
    if (isValidVideoId(raw)) return raw;

    const fromUrl = extractVideoId(raw);
    if (isValidVideoId(fromUrl)) return fromUrl;
  }

  return null;
}

function normalizePlaylistEntries(data) {
  const seenIds = new Set();
  return (data?.entries || [])
    .map(e => {
      const id = getEntryVideoId(e);
      if (!id) return null;

      return {
        id,
        title: e?.title || e?.fulltitle || e?.track || 'Unknown',
        duration: e?.duration || null,
        thumbnail: e?.thumbnail || `https://i.ytimg.com/vi/${id}/mqdefault.jpg`,
        uploader: e?.uploader || e?.channel || e?.artist || 'YouTube'
      };
    })
    .filter(Boolean)
    .filter(e => {
      if (seenIds.has(e.id)) return false;
      seenIds.add(e.id);
      return true;
    });
}

function extractPlaylistId(url) {
  if (!url) return null;
  const match = url.match(/[?&]list=([A-Za-z0-9_-]+)/);
  return match ? match[1] : null;
}

async function recoverPlaylistEntriesFromArchive(playlistId) {
  if (!playlistId) return false;

  try {
    const url = `https://www.youtube.com/playlist?list=${playlistId}`;
    const data = await invoke('fetch_playlist', { url });
    const entries = normalizePlaylistEntries(data);
    if (entries.length === 0) return false;

    const recoverCandidates = entries
      .map((song, idx) => ({ song, idx }))
      .filter(({ song }) => needsTitleRecovery(song.title));

    if (recoverCandidates.length > 0) {
      const parallel = Math.max(1, Math.min(10, Number(state.settings.repairThreads) || 4));
      await recoverTitlesBatch(entries, recoverCandidates, parallel);
    }

    const existing = state.playlists[playlistId] || {};
    state.playlists[playlistId] = {
      ...existing,
      id: playlistId,
      title: data?.title || existing.title || `Playlist ${playlistId}`,
      sourceUrl: existing.sourceUrl || url,
      songs: entries
    };
    await saveData();
    return true;
  } catch (err) {
    console.warn('Recover playlist from archive failed:', err);
    return false;
  }
}

closeRepairModal.onclick = () => {
  const currentRepair = state.activeRepairs[state.currentPlaylistId];
  repairModal.style.display = 'none';
  if (renamedTitlesModal) renamedTitlesModal.style.display = 'none';
  if (currentRepair?.status === 'checking' || currentRepair?.status === 'recovering') {
    showToast('Reparacion en segundo plano activa. Puedes seguir usando la app.');
  } else {
    showToast('Analisis pausado en resultados. Puedes reabrir cuando quieras.');
  }
  updateReopenRepairButton();
};
if (closeRenamedTitlesModal) closeRenamedTitlesModal.onclick = () => renamedTitlesModal.style.display = 'none';
if (reopenRepairBtn) reopenRepairBtn.onclick = reopenActiveRepairModal;


// ===== TOAST =====
function showToast(msg, isError = false) {
  const existing = document.querySelector('.toast');
  if (existing) existing.remove();
  const toast = document.createElement('div');
  toast.className = 'toast';
  toast.style.cssText = `
    position:fixed; bottom:110px; left:50%; transform:translateX(-50%);
    background:${isError ? '#ef4444' : 'var(--bg-elevated)'};
    border:1px solid ${isError ? '#f87171' : 'var(--glass-border)'};
    color:var(--text-primary); padding:10px 20px; border-radius:var(--radius-full);
    font-size:13px; z-index:99999; animation:toastFadeIn 0.2s ease;
    pointer-events:none;
    box-shadow:0 8px 24px rgba(0,0,0,0.4);
  `;
  toast.textContent = msg;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 3000);
}

// ===== UTILS =====
function formatDuration(seconds) {
  if (seconds === null || seconds === undefined || isNaN(seconds)) return '0:00';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);

  const sStr = s.toString().padStart(2, '0');
  if (h > 0) {
    return `${h}:${m.toString().padStart(2, '0')}:${sStr}`;
  }
  return `${m}:${sStr}`;
}

let keybindCaptureState = {
  open: false,
  editingId: null,
  capturedShortcut: ''
};

function populateKeybindActionSelect() {
  if (!keybindActionSelect) return;

  const actions = [
    { value: 'next-song', label: 'Siguiente canción' },
    { value: 'previous-song', label: 'Canción anterior' },
    { value: 'play-pause', label: 'Pausar / reproducir' },
    { value: 'favorite-add', label: 'Añadir a favoritos' },
    { value: 'notification-sound', label: 'Reproducir sonido de notificación' },
    { value: 'volume-up', label: 'Subir volumen' },
    { value: 'volume-down', label: 'Bajar volumen' }
  ];

  keybindActionSelect.innerHTML = actions
    .map(action => `<option value="${action.value}">${action.label}</option>`)
    .join('');
}

function setKeybindCaptureText(text) {
  if (capturedShortcutText) {
    capturedShortcutText.textContent = text || 'Esperando captura...';
  }
}

function openKeybindCaptureModal(binding = null) {
  if (!keybindCaptureModal) return;

  keybindCaptureState = {
    open: true,
    editingId: binding?.id || null,
    capturedShortcut: binding?.shortcut || ''
  };

  if (keybindModalTitle) {
    keybindModalTitle.textContent = binding ? 'Editar bindeo' : 'Añadir bindeo';
  }

  if (keybindActionSelect) {
    keybindActionSelect.value = binding?.action || 'play-pause';
  }

  if (keybindPressCountSelect) {
    keybindPressCountSelect.value = String(Math.max(1, Math.min(2, Number(binding?.pressCount || 1))));
  }

  setKeybindCaptureText(keybindCaptureState.capturedShortcut || 'Esperando captura...');
  keybindCaptureModal.style.display = 'flex';
}

function closeKeybindCaptureModal() {
  if (!keybindCaptureModal) return;
  keybindCaptureModal.style.display = 'none';
  keybindCaptureState = { open: false, editingId: null, capturedShortcut: '' };
  setKeybindCaptureText('Esperando captura...');
}

function renderKeybindsList() {
  if (!keybindsList) return;
  const keybinds = Array.isArray(state.settings.keybinds) ? state.settings.keybinds : [];

  if (keybinds.length === 0) {
    keybindsList.innerHTML = '<p class="queue-empty-sub">No hay bindeos configurados.</p>';
    return;
  }

  keybindsList.innerHTML = keybinds.map((binding) => {
    const shortcutLabel = escapeHTML(formatShortcutLabel(binding.shortcut));
    const actionLabel = escapeHTML(getKeybindActionLabel(binding.action));
    const pressLabel = escapeHTML(formatPressCountLabel(binding.pressCount));
    const pressCount = Math.max(1, Math.floor(Number(binding.pressCount) || 1));
    const builtinBadge = binding.builtin ? '<span class="keybind-badge">Predeterminado</span>' : '';
    const countBadge = pressCount > 1 ? `<span class="keybind-badge">${pressLabel}</span>` : '';
    return `
      <div class="keybind-card" data-keybind-id="${escapeHTML(binding.id)}">
        <div class="keybind-info">
          <div class="keybind-shortcut">${shortcutLabel} ${countBadge}</div>
          <div class="keybind-action-label">${actionLabel} ${builtinBadge}</div>
        </div>
        <div class="keybind-card-actions">
          <button class="btn-icon keybind-edit-btn" data-keybind-edit="${escapeHTML(binding.id)}" title="Editar bindeo" aria-label="Editar bindeo">
            <svg viewBox="0 0 24 24" fill="none"><path d="M4 20h4l10.5-10.5-4-4L4 16v4z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M12.5 6.5l4 4" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>
          </button>
          <button class="btn-icon keybind-delete-btn" data-keybind-delete="${escapeHTML(binding.id)}" title="Eliminar bindeo" aria-label="Eliminar bindeo">
            <svg viewBox="0 0 24 24" fill="none"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
          </button>
        </div>
      </div>
    `;
  }).join('');

  keybindsList.querySelectorAll('[data-keybind-edit]').forEach((btn) => {
    btn.onclick = () => {
      const binding = keybinds.find(item => item.id === btn.getAttribute('data-keybind-edit'));
      if (binding) openKeybindCaptureModal(binding);
    };
  });

  keybindsList.querySelectorAll('[data-keybind-delete]').forEach((btn) => {
    btn.onclick = async () => {
      const id = btn.getAttribute('data-keybind-delete');
      const binding = keybinds.find(item => item.id === id);
      if (!binding) return;
      if (!confirm(`Eliminar bindeo ${binding.shortcut}?`)) return;
      state.settings.keybinds = keybinds.filter(item => item.id !== id);
      await saveData();
      await syncKeybindsToBackend();
      renderKeybindsList();
      showToast('Bindeo eliminado');
    };
  });
}

async function syncKeybindsToBackend() {
  try {
    const keybinds = normalizeKeybinds(state.settings.keybinds);
    await invoke('sync_keybinds', { keybinds: keybinds.map(({ shortcut, action, pressCount }) => ({ shortcut, action, pressCount })) });
  } catch (e) {
    console.error('Error sincronizando bindeos:', e);
    showToast('No se pudieron sincronizar los bindeos', true);
  }
}

function upsertKeybindBinding({ shortcut, action }) {
  const normalizedShortcut = String(shortcut || '').trim();
  const normalizedAction = String(action || '').trim();
  if (!normalizedShortcut || !normalizedAction) return false;
  if (isUnsafeGlobalShortcut(normalizedShortcut)) return false;
  const pressCount = Math.max(1, Math.min(2, Number(keybindPressCountSelect?.value || 1)));

  const current = normalizeKeybinds(state.settings.keybinds);
  const editedId = keybindCaptureState.editingId;

  if (editedId) {
    const existingIndex = current.findIndex(item => item.id === editedId);
    const updatedItem = {
      id: editedId,
      shortcut: normalizedShortcut,
      action: normalizedAction,
      pressCount,
      builtin: false
    };

    if (existingIndex >= 0) {
      current[existingIndex] = {
        ...current[existingIndex],
        ...updatedItem
      };
    } else {
      current.push(updatedItem);
    }

    state.settings.keybinds = current;
    return true;
  }

  const updatedItem = {
    id: createKeybindId(),
    shortcut: normalizedShortcut,
    action: normalizedAction,
    pressCount,
    builtin: false
  };

  current.push(updatedItem);
  state.settings.keybinds = current;
  return true;
}

async function commitKeybindFromModal() {
  const shortcut = String(keybindCaptureState.capturedShortcut || '').trim();
  const action = String(keybindActionSelect?.value || '').trim();

  if (!shortcut) {
    showToast('Captura una tecla o combinación antes de confirmar', true);
    return;
  }

  if (!action) {
    showToast('Selecciona una acción', true);
    return;
  }

  if (isUnsafeGlobalShortcut(shortcut)) {
    showToast('Las teclas sueltas como S no se pueden usar como bindeo global. Usa Ctrl+S, Alt+S, NumpadAdd/NumpadMultiply/NumpadDivide, F1 o una tecla multimedia.', true);
    return;
  }

  const updated = upsertKeybindBinding({ shortcut, action });
  if (!updated) return;

  await saveData();
  await syncKeybindsToBackend();
  renderKeybindsList();
  closeKeybindCaptureModal();
  showToast('Bindeo guardado');
}

function handleKeybindCaptureKeydown(event) {
  if (!keybindCaptureState.open) return;

  const shortcut = normalizeShortcutFromEvent(event);
  if (!shortcut) return;

  if (isUnsafeGlobalShortcut(shortcut)) {
    event.preventDefault();
    event.stopPropagation();
    showToast('Las teclas sueltas como S no se pueden usar como bindeo global. Usa Ctrl+S, Alt+S, NumpadAdd/NumpadMultiply/NumpadDivide, F1 o una tecla multimedia.', true);
    return;
  }

  event.preventDefault();
  event.stopPropagation();
  keybindCaptureState.capturedShortcut = shortcut;
  setKeybindCaptureText(shortcut);
}

async function performQuickSearch(query, options = {}) {
  const {
    enqueueOnly = false,
    autoTrigger = false,
    silent = false,
    replaceQueue = false
  } = options;
  console.log('AUTO: Performing quick search for:', query);
  try {
    const actualQuery = state.searchMode === 'youtube_music' ? `${query} music` : query;
    const results = await invoke('search_youtube', { query: actualQuery });

    const normalizedResults = Array.isArray(results)
      ? results
          .map((r) => {
            if (!r) return null;
            const id = r.id || r.video_id || extractVideoId(r.url || r.webpage_url || '');
            if (!id) return null;
            return {
              ...r,
              id,
              title: r.title || 'Unknown',
              thumbnail: r.thumbnail || `https://i.ytimg.com/vi/${id}/mqdefault.jpg`,
              uploader: r.uploader || r.channel || 'YouTube',
              duration: r.duration || null
            };
          })
          .filter(Boolean)
      : [];

    if (normalizedResults.length > 0) {
      const durationFiltered = normalizedResults.filter(r => isWithinMaxDuration(r));
      const pool = durationFiltered.length > 0 ? durationFiltered : normalizedResults;
      const nonBlacklisted = pool.filter(r => !isBlacklisted(r));
      let bestMatch = nonBlacklisted.find(r => !state.queue.some(q => q.id === r.id));
      if (!bestMatch) bestMatch = nonBlacklisted[0] || pool[0];
      if (!bestMatch || !bestMatch.id) return false;

      if (enqueueOnly) {
        const idx = addToQueue(bestMatch, {
          allowDuplicate: true,
          showDuplicateToast: false,
          showAddedToast: false
        });
        if (idx === -1) return false;

        if (autoTrigger && !state.isPlaying) {
          playFromQueue(idx);
          if (!silent) showToast(`AUTO: Reproduciendo ${bestMatch.title}`);
        } else {
          if (!silent) showToast(`AUTO: Añadido a la cola: ${bestMatch.title}`);
        }
      } else {
        if (replaceQueue) {
          state.queue = [bestMatch];
          state.currentIndex = 0;
          updateQueueUI();
        }

        playVideo(bestMatch);
        if (!silent) {
          showToast(replaceQueue
            ? `AUTO: Cola reiniciada con ${bestMatch.title}`
            : `AUTO: Reproduciendo ${bestMatch.title}`);
        }
      }

      // Clear input after quick search
      if (!state.settings.autoTheme && autoThemeInput) autoThemeInput.value = '';
      schedulePlaybackSessionSave(0);
      return true;
    }
  } catch (e) {
    console.error('Quick search failed:', e);
  }
  return false;
}

async function performAutoMixSearch(videoId, options = {}) {
  const {
    enqueueOnly = false,
    autoTrigger = false,
    silent = false,
    mixSize = 10,
    desiredCount = 1
  } = options;

  if (!isValidVideoId(videoId)) return false;

  try {
    const limit = Math.max(1, Math.min(100, Math.floor(Number(mixSize) || 10)));
    const results = await invoke('get_auto_mix_candidates', { videoId, limit });

    const normalizedResults = Array.isArray(results)
      ? results
          .map((r) => {
            if (!r) return null;
            const id = r.id || r.video_id || extractVideoId(r.url || r.webpage_url || '');
            if (!id || !isValidVideoId(id)) return null;
            return {
              ...r,
              id,
              title: r.title || 'Unknown',
              thumbnail: r.thumbnail || `https://i.ytimg.com/vi/${id}/mqdefault.jpg`,
              uploader: r.uploader || r.channel || 'YouTube',
              duration: r.duration || null
            };
          })
          .filter(Boolean)
      : [];

    if (normalizedResults.length === 0) return false;

    const durationFiltered = normalizedResults.filter(r => isWithinMaxDuration(r));
    const pool = durationFiltered.length > 0 ? durationFiltered : normalizedResults;
    const nonBlacklisted = pool.filter(r => !isBlacklisted(r));
    if (nonBlacklisted.length === 0) return false;

    const wanted = Math.max(1, Math.min(100, Math.floor(Number(desiredCount) || 1)));
    const existingIds = new Set((state.queue || []).map(q => q?.id).filter(Boolean));

    const uniqueCandidates = nonBlacklisted.filter(r => !existingIds.has(r.id));
    const fallbackCandidates = nonBlacklisted.filter(r => existingIds.has(r.id));
    const candidateList = uniqueCandidates.length > 0
      ? uniqueCandidates.concat(fallbackCandidates)
      : fallbackCandidates;

    const firstMatch = candidateList[0];
    if (!firstMatch || !firstMatch.id) return false;

    if (!enqueueOnly) {
      playVideo(firstMatch);
      if (!silent) showToast(`AUTO MIX: Reproduciendo ${firstMatch.title}`);
      schedulePlaybackSessionSave(0);
      return true;
    }

    let addedCount = 0;
    let firstAddedIndex = -1;
    let firstAddedTitle = '';

    for (const candidate of candidateList) {
      if (!candidate?.id) continue;
      const idx = addToQueue(candidate, {
        allowDuplicate: true,
        showDuplicateToast: false,
        showAddedToast: false
      });
      if (idx === -1) continue;

      addedCount += 1;
      if (firstAddedIndex === -1) {
        firstAddedIndex = idx;
        firstAddedTitle = candidate.title || 'Unknown';
      }

      if (addedCount >= wanted) break;
    }

    if (addedCount === 0) return false;

    if (autoTrigger && !state.isPlaying && firstAddedIndex >= 0) {
      playFromQueue(firstAddedIndex);
      if (!silent) showToast(`AUTO MIX: Reproduciendo ${firstAddedTitle}`);
    } else if (!silent) {
      if (addedCount === 1) showToast(`AUTO MIX: Añadido a la cola: ${firstAddedTitle}`);
      else showToast(`AUTO MIX: Añadidas ${addedCount} canciones a la cola`);
    }

    schedulePlaybackSessionSave(0);
    return true;
  } catch (e) {
    console.error('Auto mix search failed:', e);
  }

  return false;
}

// ===== YOUTUBE OAUTH =====
async function checkAuthStatus() {
  try {
    const authData = await invoke('load_data', { filename: 'auth.json' });
    const sidebarUserBtn = document.getElementById('sidebarUserBtn');

    if (authData && authData.access_token) {
      ytAuthStatus.style.display = 'none';
      ytUserInfo.style.display = 'block';
      ytUserName.textContent = 'Conectado a YouTube';
      if (sidebarUserBtn) {
        sidebarUserBtn.style.display = '';
        sidebarUserBtn.onclick = () => {
          if (confirm('¿Quieres cerrar sesión de YouTube?')) logoutYtBtn.click();
        };
      }
      // No longer auto-fetching playlists here based on user preference
      myPlaylistsList.innerHTML = `<p class="sidebar-hint">Haz clic en recargar (arriba) para ver tus playlists de YouTube.</p>`;
    } else {
      ytAuthStatus.style.display = 'block';
      ytUserInfo.style.display = 'none';
      if (sidebarUserBtn) sidebarUserBtn.style.display = 'none';
      myPlaylistsList.innerHTML = `
        <div class="sidebar-auth-hint">
          <p class="sidebar-hint">Conecta tu cuenta para ver tus playlists de YouTube aquí.</p>
          <button id="sidebarLoginYtBtn" class="btn-google btn-sm">
            <svg viewBox="0 0 24 24" width="16" height="16"><path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z" fill="#4285F4"/><path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853"/><path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l3.66-2.84z" fill="#FBBC05"/><path d="M12 5.38c1.62 0 3.06.56 4.21 1.66l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335"/></svg>
            <span>Acceder</span>
          </button>
        </div>
      `;
      // Re-bind sidebar login button
      const newSidebarBtn = document.getElementById('sidebarLoginYtBtn');
      if (newSidebarBtn) newSidebarBtn.onclick = () => loginYtBtn.click();
    }
  } catch (e) {
    console.warn('Auth check failed:', e);
  }
}

async function refreshAccessToken() {
  try {
    const authData = await invoke('load_data', { filename: 'auth.json' });
    if (authData && authData.refresh_token) {
      console.log('AUTH: Refreshing access token...');
      const newData = await invoke('refresh_oauth_token', { refreshToken: authData.refresh_token });
      return !!newData.access_token;
    }
  } catch (e) {
    console.warn('AUTH: Refresh failed:', e);
  }
  return false;
}

async function invokeYt(cmd, args = {}) {
  try {
    let res = await invoke(cmd, args);
    if (res && res.error && res.error.code === 401) {
      console.warn(`AUTH: Command ${cmd} failed with 401, refreshing...`);
      if (await refreshAccessToken()) {
        res = await invoke(cmd, args);
      }
    }
    return res;
  } catch (err) {
    if (err.toString().includes('401')) {
      console.warn(`AUTH: Command ${cmd} threw 401, refreshing...`);
      if (await refreshAccessToken()) {
        return await invoke(cmd, args);
      }
    }
    throw err;
  }
}

function stringifyError(err) {
  if (!err) return '';
  if (typeof err === 'string') return err;
  if (err?.message) return String(err.message);
  try {
    return JSON.stringify(err);
  } catch (_) {
    return String(err);
  }
}

function isPlaylistNotFoundError(err) {
  const text = stringifyError(err).toLowerCase();
  return (
    text.includes('playlistnotfound') ||
    (text.includes('"code": 404') && text.includes('playlistid')) ||
    (text.includes('code') && text.includes('404') && text.includes('playlist identified'))
  );
}

// Estos listeners ahora se asignan dentro de init() para mayor seguridad


async function fetchMyPlaylists() {
  const currentHTML = myPlaylistsList.innerHTML;
  myPlaylistsList.innerHTML = '<div class="loading-spinner" style="margin:20px auto"></div>';
  try {
    let data = await invokeYt('get_youtube_playlists');

    if (data.error && data.error.code === 401) {
      throw new Error("Token expirado o inválido");
    }
    renderMyPlaylists(data.items || []);
  } catch (err) {
    console.error("fetchMyPlaylists error:", err);
    showToast('Sesión caducada. Por favor, vuelve a iniciar sesión.', true);

    // Auto-logout internally when fetch fails
    try { await invoke('save_data', { filename: 'auth.json', data: {} }); } catch (e) { }
    checkAuthStatus();
  }
}

function renderMyPlaylists(playlists) {
  if (playlists.length === 0) {
    myPlaylistsList.innerHTML = '<p class="sidebar-hint">No tienes playlists creadas</p>';
    return;
  }
  myPlaylistsList.innerHTML = '';
  playlists.forEach(pl => {
    const item = document.createElement('div');
    item.className = 'playlist-item';
    item.innerHTML = `
      <svg viewBox="0 0 24 24" fill="currentColor"><path d="M19.65 8.54c-1.18-1.54-3.5-1.54-3.5-1.54H7.85s-2.32 0-3.5 1.54c-1.18 1.54-1.18 4.46-1.18 4.46s0 2.92 1.18 4.46c1.18 1.54 3.5 1.54 3.5 1.54h8.3s2.32 0 3.5-1.54c1.18-1.54 1.18-4.46 1.18-4.46s0-2.92-1.18-4.46zM10 14.5v-5l4.5 2.5-4.5 2.5z"/></svg>
      <span class="playlist-name">${escapeHTML(pl.snippet.title)}</span>
      <div class="playlist-actions">
        <button class="playlist-action-btn sync-local" title="Sincronizar hacia Local">
          <svg viewBox="0 0 24 24" fill="none"><path d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </button>
        <button class="playlist-action-btn play-now" title="Reproducir ahora">
          <svg viewBox="0 0 24 24" fill="none"><path d="M5 3l14 9-14 9V3z" fill="currentColor"/></svg>
        </button>
      </div>
    `;
    item.onclick = (e) => {
      const btn = e.target.closest('.playlist-action-btn');
      if (btn) {
        e.stopPropagation();
        if (btn.classList.contains('sync-local')) startSyncFlow(pl.id, 'to_local');
        else if (btn.classList.contains('play-now')) playPlaylistNowYt(pl.id);
        return;
      }
      // View this specific playlist
      selectYtPlaylist(pl.id, pl.snippet.title);
    };
    myPlaylistsList.appendChild(item);
  });
}

async function addCurrentToYtPlaylist(playlist) {
  const current = state.queue[state.currentIndex];
  if (!current) {
    showToast('No hay nada reproduciéndose', true);
    return;
  }

  const confirmed = await showConfirm(`¿Añadir "${current.title}" a la playlist "${playlist.snippet.title}"?`);
  if (!confirmed) return;

  showToast('Añadiendo a YouTube...');
  try {
    await invokeYt('youtube_add_to_playlist', { playlistId: playlist.id, videoId: current.id });
    showToast('Añadido con éxito');
  } catch (err) {
    showToast('Error: ' + err, true);
  }
}

// ===== CONFIRMATION MODAL UTILS =====
function showConfirm(message) {
  return new Promise((resolve) => {
    confirmMessage.textContent = message;
    confirmModal.style.display = 'flex';

    const cleanup = (value) => {
      confirmModal.style.display = 'none';
      okConfirmBtn.onclick = null;
      cancelConfirmBtn.onclick = null;
      closeConfirmModal.onclick = null;
      resolve(value);
    };

    okConfirmBtn.onclick = () => cleanup(true);
    cancelConfirmBtn.onclick = () => cleanup(false);
    closeConfirmModal.onclick = () => cleanup(false);
  });
}

async function selectYtPlaylist(id, title) {
  state.currentPlaylistId = id; // e.g. PL...
  renderPlaylists();

  showScreen('loading');
  loadingQuery.textContent = `playlist "${title}"`;

  try {
    const data = await invokeYt('get_youtube_playlist_items', { playlistId: id });
    const songs = (data.items || []).map(i => ({
      id: i.snippet?.resourceId?.videoId || i.contentDetails?.videoId,
      title: i.snippet?.title || 'Unknown',
      thumbnail: i.snippet?.thumbnails?.default?.url || i.snippet?.thumbnails?.high?.url || '',
      uploader: i.snippet?.videoOwnerChannelTitle || 'YouTube'
    })).filter(s => s.id);

    state.lastSearchResults = songs;

    showScreen('playlist');
    resultsTitle.textContent = title;
    resultsCount.textContent = `${songs.length} Canciones`;
    renderCards(songs);
    renderHeaderActions();
  } catch (err) {
    showScreen('error');
    errorMessage.textContent = err.toString();
  }
}

async function playPlaylistNowYt(id) {
  showToast('Cargando playlist...');
  try {
    const data = await invokeYt('get_youtube_playlist_items', { playlistId: id });
    const songs = (data.items || []).map(i => ({
      id: i.snippet?.resourceId?.videoId || i.contentDetails?.videoId,
      title: i.snippet?.title || 'Unknown',
      thumbnail: i.snippet?.thumbnails?.default?.url || i.snippet?.thumbnails?.high?.url || '',
      uploader: i.snippet?.videoOwnerChannelTitle || 'YouTube'
    })).filter(s => s.id);

    if (songs.length === 0) return;
    state.queue = [...songs];
    state.currentIndex = 0;
    playFromQueue(0);
  } catch (err) {
    showToast('Error: ' + err, true);
  }
}

function escapeHTML(str) {
  if (!str) return '';
  const p = document.createElement('p');
  p.textContent = str;
  return p.innerHTML;
}

async function search(query) {
  if (!query) return;
  state.lastQuery = query;
  showScreen('loading');
  loadingQuery.textContent = query;
  searchSuggestions.style.display = 'none';

  try {
    const actualQuery = state.searchMode === 'youtube_music' ? `${query} music` : query;
    const results = await invoke('search_youtube', { query: actualQuery });
    const filteredResults = Array.isArray(results) ? results.filter(r => isWithinMaxDuration(r)) : [];
    state.lastSearchResults = filteredResults;

    showScreen('results');
    resultsTitle.textContent = `Resultados para "${query}"`;
    resultsCount.textContent = `${filteredResults.length} encontrados`;
    renderCards(filteredResults);
    renderHeaderActions();
  } catch (err) {
    showScreen('error');
    errorMessage.textContent = err.toString();
  }
}

async function getSuggestions(query) {
  if (!query || query.length < 2) {
    searchSuggestions.style.display = 'none';
    return;
  }
  try {
    const suggestions = await invoke('get_suggestions', { query });
    if (suggestions && suggestions.length > 0) {
      searchSuggestions.innerHTML = suggestions.map(s => `
        <div class="suggestion-item">
          <svg viewBox="0 0 24 24" fill="none"><circle cx="11" cy="11" r="8" stroke="currentColor" stroke-width="2"/><path d="m21 21-4.35-4.35" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
          <span>${escapeHTML(s)}</span>
        </div>
      `).join('');
      searchSuggestions.style.display = 'block';

      searchSuggestions.querySelectorAll('.suggestion-item').forEach((item, idx) => {
        item.onclick = () => {
          searchInput.value = suggestions[idx];
          search(suggestions[idx]);
        };
      });
    } else {
      searchSuggestions.style.display = 'none';
    }
  } catch (e) { console.error(e); }
}

function setSearchMode(mode, reference = '') {
  state.searchMode = mode;
  state.searchReference = reference;

  modeIconYt.style.display = mode === 'youtube' ? 'block' : 'none';
  modeIconYtm.style.display = mode === 'youtube_music' ? 'block' : 'none';

  if (reference) {
    if (typeof reference === 'object') {
      if (reference.text) {
        searchReferenceEl.textContent = String(reference.text);
      } else if (reference.highlight) {
        const label = escapeHTML(reference.label || 'Sugerencias de');
        const highlight = escapeHTML(reference.highlight);
        searchReferenceEl.innerHTML = `${label} (<strong style="color: var(--accent);">${highlight}</strong>)`;
      } else {
        searchReferenceEl.textContent = String(reference);
      }
    } else {
      searchReferenceEl.textContent = String(reference);
    }
    searchReferenceEl.style.display = 'block';
  } else {
    searchReferenceEl.style.display = 'none';
  }
}

async function deletePlaylist(id) {
  const confirmed = await showConfirm(`¿Estás seguro de que quieres eliminar la playlist "${state.playlists[id].title}"?`);
  if (!confirmed) return;

  delete state.playlists[id];
  await saveData();
  renderPlaylists();
  if (state.currentPlaylistId === id) showScreen('welcome');
  showToast("Playlist eliminada");
}

function openPlaylistSelectModal(video) {
  playlistSelectList.innerHTML = '';

  // Add Favorites option
  const favItem = document.createElement('div');
  favItem.className = 'playlist-select-item';
  favItem.innerHTML = '<span>❤️ Favoritos Locales</span>';
  favItem.onclick = () => {
    void addSongToFavorites(video, { updateCurrentHeart: false });
    playlistSelectModal.style.display = 'none';
  };
  playlistSelectList.appendChild(favItem);

  // Add other playlists
  Object.keys(state.playlists).forEach(id => {
    const pl = state.playlists[id];
    const item = document.createElement('div');
    item.className = 'playlist-select-item';
    item.innerHTML = `<span>📂 ${escapeHTML(pl.title)}</span>`;
    item.onclick = () => {
      if (!pl.songs.some(s => s.id === video.id)) {
        pl.songs.push(video);
        saveData();
        showToast(`Añadido a ${pl.title}`);
      } else {
        showToast("Ya está en la playlist");
      }
      playlistSelectModal.style.display = 'none';
    };
    playlistSelectList.appendChild(item);
  });

  playlistSelectModal.style.display = 'flex';
}

function updateProgressBar() {
  if (!audio.duration) return;
  const pct = (audio.currentTime / audio.duration) * 100;
  progressFill.style.width = `${pct}%`;
  progressThumb.style.left = `${pct}%`;
  currentTimeEl.textContent = formatDuration(audio.currentTime);
  totalTimeEl.textContent = formatDuration(audio.duration);

  if (npProgressFill) npProgressFill.style.width = `${pct}%`;
  if (npCurrentTime) npCurrentTime.textContent = formatDuration(audio.currentTime);
  if (npTotalTime) npTotalTime.textContent = formatDuration(audio.duration);
}

// ===== SYNC LOGIC =====
async function startSyncFlow(id, direction) {
  syncModal.style.display = 'flex';
  syncModalTitle.textContent = direction === 'to_youtube' ? 'Sincronizar a YouTube' : 'Sincronizar de YouTube';
  syncModalDesc.textContent = 'Comparando contenidos...';
  syncAddList.innerHTML = '';
  syncRemoveList.innerHTML = '';
  syncReplaceList.innerHTML = '';
  syncReplaceGroup.style.display = 'none';
  syncMatchCount.textContent = '0';
  syncAddCount.textContent = '0';
  syncRemoveCount.textContent = '0';
  applySyncBtn.onclick = null;
  applySyncBtn.disabled = true;

  try {
    let sourceSongs = [];
    let destSongs = [];
    let sourceTitle = "";
    let destTitle = "";
    let targetPlaylistId = "";

    if (direction === 'to_youtube') {
      // Local -> YouTube
      const localPl = state.playlists[id];
      if (!localPl) throw "Playlist local no encontrada";
      sourceSongs = localPl.songs;
      sourceTitle = localPl.title;

      const ytPlaylists = await invokeYt('get_youtube_playlists');
      let targetYtPl = (ytPlaylists.items || []).find(pl => pl.id === id);

      if (!targetYtPl) {
        showToast('Creando playlist en YouTube...');
        const newYtId = await invokeYt('youtube_create_playlist', { title: localPl.title });

        // Remap the local playlist ID to the new YouTube ID so they stay linked
        state.playlists[newYtId] = state.playlists[id];
        delete state.playlists[id];
        await saveData();
        renderPlaylists();

        targetPlaylistId = newYtId;
        destTitle = localPl.title;
      } else {
        targetPlaylistId = id;
        destTitle = localPl.title;
      }

      const ytItems = await invokeYt('get_youtube_playlist_items', { playlistId: targetPlaylistId });
      destSongs = (ytItems.items || []).map(i => ({
        id: i.snippet?.resourceId?.videoId || i.contentDetails?.videoId,
        title: i.snippet?.title || 'Unknown'
      })).filter(s => s.id);
    } else {
      // YouTube -> Local
      let ytPl = null;
      try {
        ytPl = await invokeYt('get_youtube_playlist_items', { playlistId: id });
      } catch (err) {
        if (isPlaylistNotFoundError(err)) {
          syncModalDesc.textContent = 'Playlist no encontrada en YouTube. Intentando recuperar desde archivo...';
          const recovered = await recoverPlaylistEntriesFromArchive(id);
          if (recovered && state.playlists[id]?.songs?.length) {
            sourceSongs = state.playlists[id].songs;
            sourceTitle = state.playlists[id].title || `Playlist ${id}`;
          } else {
            syncModalDesc.textContent = 'No se puede sincronizar: la playlist ya no existe en YouTube y no se pudo recuperar.';
            return;
          }
        } else {
          throw err;
        }
      }

      if (!sourceSongs.length) {
        sourceSongs = (ytPl?.items || []).map(i => ({
          id: i.snippet?.resourceId?.videoId || i.contentDetails?.videoId,
          title: i.snippet?.title || 'Unknown',
          thumbnail: i.snippet?.thumbnails?.default?.url || '',
          uploader: i.snippet?.videoOwnerChannelTitle || 'YouTube'
        })).filter(s => s.id);
      }

      const ytPlaylists = await invokeYt('get_youtube_playlists');
      const ytPlistInfo = (ytPlaylists.items || []).find(pl => pl.id === id);
      sourceTitle = sourceTitle || ytPlistInfo?.snippet?.title || 'Playlist YouTube';

      if (state.playlists[id]) {
        destSongs = state.playlists[id].songs;
        destTitle = state.playlists[id].title;
        targetPlaylistId = id;
      } else {
        destTitle = sourceTitle + ' (Local)';
        targetPlaylistId = id;
      }
    }

    // Calculate differences
    const matches = [];
    const toAdd = [];
    const toRemove = [];
    const toReplace = [];

    sourceSongs.forEach(src => {
      const match = destSongs.find(d => d.id === src.id);
      if (match) matches.push({ src, dest: match });
      else toAdd.push(src);
    });

    destSongs.forEach(dst => {
      if (!sourceSongs.find(s => s.id === dst.id)) toRemove.push(dst);
    });

    syncModalDesc.textContent = `Diferencias encontradas: ${matches.length} coincidencias, ${toAdd.length} por añadir, ${toRemove.length} por eliminar`;
    syncMatchCount.textContent = matches.length;
    syncAddCount.textContent = toAdd.length;
    syncRemoveCount.textContent = toRemove.length;
    applySyncBtn.disabled = false;

    toAdd.forEach(s => {
      const item = document.createElement('div');
      item.className = 'sync-item';
      item.innerHTML = `<span>${escapeHTML(s.title)}</span>`;
      syncAddList.appendChild(item);
    });

    toRemove.forEach(s => {
      const item = document.createElement('div');
      item.className = 'sync-item';
      item.innerHTML = `<span>${escapeHTML(s.title)}</span>`;
      syncRemoveList.appendChild(item);
    });

    applySyncBtn.onclick = async () => {
      applySyncBtn.disabled = true;
      syncModalDesc.textContent = 'Aplicando cambios...';
      try {
        if (direction === 'to_youtube') {
          for (const s of toAdd) {
            await invokeYt('youtube_add_to_playlist', { playlistId: targetPlaylistId, videoId: s.id });
          }
        } else {
          if (!state.playlists[targetPlaylistId]) {
            state.playlists[targetPlaylistId] = { id: targetPlaylistId, title: destTitle, songs: [] };
          }
          toAdd.forEach(s => {
            if (!state.playlists[targetPlaylistId].songs.find(song => song.id === s.id)) {
              state.playlists[targetPlaylistId].songs.push(s);
            }
          });
          await saveData();
        }
        syncModalDesc.textContent = '✓ Sincronización completada';
        setTimeout(() => {
          syncModal.style.display = 'none';
          applySyncBtn.disabled = false;
          renderPlaylists();
          showToast('Sincronización exitosa');
        }, 1500);
      } catch (err) {
        syncModalDesc.textContent = '✗ Error: ' + err;
        applySyncBtn.disabled = false;
      }
    };
  } catch (err) {
    syncModalDesc.textContent = 'Error: ' + err;
    console.error('Sync error:', err);
    applySyncBtn.disabled = false;
  }
}

// ===== START INITIALIZATION =====
document.addEventListener('DOMContentLoaded', async () => {
  // Show verification status
  ytdlpStatus.className = 'status-badge status-checking';
  statusText.innerText = 'Verificando...';
  
  // Load build/dev info for status tag
  try {
    const versionInfo = await invoke('get_version_info');
    state.versionInfo = versionInfo;
    statusText.innerText = getBuildStatusTag(versionInfo);
  } catch (e) {
    console.log('Version info not available:', e);
  }

  // Initialize app
  await init();
});

async function recoverTitlesBatch(entries, recoverCandidates, concurrency) {
  let cursor = 0;
  const total = recoverCandidates.length;

  const worker = async () => {
    while (true) {
      const taskIndex = cursor;
      cursor += 1;
      if (taskIndex >= total) break;

      const { song } = recoverCandidates[taskIndex];
      try {
        const title = normalizeRecoveredTitle(await recoverTitleForVideoId(song.id, song.title));
        if (isValidTitle(title)) {
          song.title = title;
        }
      } catch (err) {
        console.warn(`Batch recovery failed for ${song.id}:`, err);
      }
    }
  };

  await Promise.all(Array.from({ length: Math.min(concurrency, total) }, () => worker()));
}

function normalizeRecoveredTitle(title) {
  if (!title) return '';
  return title
    .replace(/&quot;/g, '"')
    .replace(/&amp;/g, '&')
    .replace(/&#39;/g, "'")
    .replace(/\s+/g, ' ')
    .trim();
}

function isValidTitle(title) {
  if (!title || typeof title !== 'string') return false;
  const t = title.toLowerCase();
  if (t === 'unknown' || t === 'video unknown' || t === '[deleted video]') return false;
  // Many broken songs have titles like "Video dQw4w9WgXcQ"
  if (t.startsWith('video ') && t.length <= 17) return false; 
  if (t === 'private video' || t === 'deleted video') return false;
  return t.length > 3;
}

function isValidRecoveredTitle(title) {
  return isValidTitle(title);
}

function needsTitleRecovery(title) {
  return !isValidTitle(title);
}