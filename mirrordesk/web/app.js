/* MirrorDesk browser client. No build step, no third-party code, CSP 'self'. */
'use strict';
(function () {
  const $ = (id) => document.getElementById(id);
  const video = $('video'), stage = $('stage');
  const store = {
    get(key, fallback) { try { const v = localStorage.getItem('mirrordesk.' + key); return v === null ? fallback : JSON.parse(v); } catch (e) { return fallback; } },
    set(key, value) { try { localStorage.setItem('mirrordesk.' + key, JSON.stringify(value)); } catch (e) { /* private mode */ } },
  };

  function timeoutSignal(ms) {
    if (window.AbortSignal && AbortSignal.timeout) return AbortSignal.timeout(ms);
    const controller = new AbortController();
    setTimeout(() => controller.abort(), ms);
    return controller.signal;
  }

  async function api(path, options) {
    options = options || {};
    const init = { method: options.method || 'GET', headers: {}, signal: timeoutSignal(options.timeout || 15000), credentials: 'same-origin' };
    if (init.method !== 'GET') init.headers['X-MirrorDesk'] = '1';
    if (options.body !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(options.body); }
    if (options.keepalive) init.keepalive = true;
    const response = await fetch(path, init);
    if (response.status === 401) { location.href = '/login'; throw new Error('Sign in required'); }
    if (!response.ok) {
      let text = '';
      try { text = await response.text(); } catch (e) { /* ignore */ }
      const error = new Error(text || ('HTTP ' + response.status));
      error.status = response.status;
      throw error;
    }
    const type = response.headers.get('Content-Type') || '';
    return type.includes('application/json') ? response.json() : response;
  }

  let toastTimer;
  function toast(text, ms) {
    const el = $('toast');
    el.textContent = text; el.hidden = !text;
    clearTimeout(toastTimer);
    if (text && ms !== 0) toastTimer = setTimeout(() => { el.hidden = true; }, ms || 4000);
  }

  function setChip(id, text, tone) {
    const chip = $(id);
    chip.querySelector('.value').textContent = text;
    chip.dataset.tone = tone;
  }

  function setBadge(id, text, tone) {
    $(id).textContent = text;
    $(id).dataset.tone = tone;
  }

  const SVG = 'http://www.w3.org/2000/svg';
  function icon(name) {
    const svg = document.createElementNS(SVG, 'svg');
    const use = document.createElementNS(SVG, 'use');
    use.setAttribute('href', '#i-' + name);
    svg.appendChild(use);
    return svg;
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  // ---------------------------------------------------------------- viewer
  // This browser watches only while a phone is connected: it joins when the phone starts and
  // leaves a few seconds after it stops, so it never holds a viewer place (or shows a player
  // bar or "tap to play") with nothing to watch.
  const viewer = {
    pc: null, id: null, mode: null, codec: null, wanted: true, generation: 0, connecting: false,
    retry: 0, retryTimer: null, statsTimer: null, last: null, watchdog: null, compatTried: false,
    sessionCompat: false, decodedEver: false, idleTimer: null, stream: null, audioDropouts: 0,
  };
  const IDLE_DISCONNECT_MS = 3000;

  // Automatic fallbacks are remembered per browser for a week, then retried, so a
  // one-off failure or a later browser update does not pin lower quality forever.
  const FALLBACK_MS = 7 * 24 * 3600 * 1000;
  function fallback(key) { const at = store.get(key, null); return typeof at === 'number' && Date.now() - at < FALLBACK_MS; }
  function requestedMode() {
    const chosen = $('mode').value;
    if (chosen === 'auto' && (viewer.sessionCompat || fallback('forceCompat'))) return 'compat';
    return chosen;
  }

  // Something to watch: the phone streams, its screen is unchanged (it sends nothing then),
  // or only its sound plays.
  function hasMedia(s) { return !!s && ['streaming', 'paused', 'audio_only'].includes(s.source.state); }

  // MirrorDesk's own TURN relay on this page's port (turn.py). Browsers use it only when the direct
  // connection fails: a VPN or firewall that lets the page through but not the video.
  function iceServers() {
    const turn = status && status.turn;
    if (!turn) return [];
    const port = location.port || (location.protocol === 'https:' ? '443' : '80');
    return [{ urls: 'turn:' + location.hostname + ':' + port + '?transport=tcp', username: turn.username, credential: turn.credential }];
  }

  // Diagnostics categories (fixed lists on the server): which browser and device this is.
  function clientKind() {
    const ua = navigator.userAgent || '';
    const browser = navigator.brave ? 'brave' : /Edg\//.test(ua) ? 'edge' : /OPR\/|Opera/.test(ua) ? 'opera'
      : /SamsungBrowser/.test(ua) ? 'samsung' : /Firefox\/|FxiOS/.test(ua) ? 'firefox' : /Chrome\/|CriOS/.test(ua) ? 'chrome'
      : /Safari\//.test(ua) ? 'safari' : 'other';
    const device = /iPhone/.test(ua) ? 'iphone' : (/iPad/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1)) ? 'ipad'
      : /Android/.test(ua) ? 'android' : /CrOS/.test(ua) ? 'chromeos' : /Windows/.test(ua) ? 'windows'
      : /Macintosh/.test(ua) ? 'mac' : /Linux/.test(ua) ? 'linux' : 'other';
    return { browser, device };
  }

  async function waitForIce(pc, ms) {
    if (pc.iceGatheringState === 'complete') return;
    await new Promise((resolve) => {
      const done = () => { pc.removeEventListener('icegatheringstatechange', check); resolve(); };
      const check = () => { if (pc.iceGatheringState === 'complete') done(); };
      pc.addEventListener('icegatheringstatechange', check);
      setTimeout(done, ms);
    });
  }

  async function connect() {
    if (!window.RTCPeerConnection) { toast('This browser cannot play the video (no WebRTC support).', 0); return; }
    clearTimeout(viewer.retryTimer); viewer.retryTimer = null;
    viewer.connecting = true;
    viewer.wanted = true;
    // Claim a generation BEFORE awaiting teardown: any overlapping connect()
    // supersedes this one, and every superseded attempt closes its own peer.
    const generation = ++viewer.generation;
    await teardown();
    if (generation !== viewer.generation) return;
    const pc = new RTCPeerConnection({ iceServers: iceServers(), bundlePolicy: 'max-bundle',
      iceTransportPolicy: store.get('relayOnly', false) ? 'relay' : 'all' });   // relayOnly: tests
    const superseded = () => {
      if (generation === viewer.generation) return false;
      pc.close();
      return true;
    };
    viewer.pc = pc;
    viewer.stream = new MediaStream();
    pc.addTransceiver('video', { direction: 'recvonly' });
    pc.addTransceiver('audio', { direction: 'recvonly' });
    pc.ontrack = (event) => {
      if (pc !== viewer.pc) return;
      viewer.stream.addTrack(event.track);
      // Attached only once media exists: an empty stream would hold back the page's load event.
      if (video.srcObject !== viewer.stream) video.srcObject = viewer.stream;
      play();
    };
    pc.onconnectionstatechange = () => {
      if (pc !== viewer.pc) return;
      const state = pc.connectionState;
      if (state === 'connected') { viewer.retry = 0; toast(''); }
      if (state === 'failed' || state === 'closed') scheduleReconnect('Connection lost');
      if (state === 'disconnected') {
        setTimeout(() => { if (pc === viewer.pc && pc.connectionState === 'disconnected') scheduleReconnect('Connection interrupted'); }, 4000);
      }
    };
    try {
      await pc.setLocalDescription(await pc.createOffer());
      await waitForIce(pc, 3000);   // the relay's candidate takes a round trip or two
      if (superseded()) return;
      if (!/^a=candidate:/m.test(pc.localDescription.sdp)) {
        // No local address at all: the browser's privacy settings block WebRTC (Brave with
        // "Disable non-proxied UDP", some extensions, a few corporate setups).
        toast('This browser blocks the video connection (WebRTC). In Brave: Settings → Privacy and security → ' +
              'WebRTC IP handling policy → Default. Extensions that "prevent WebRTC leaks" do the same.', 0);
      }
      const answer = await api('/api/viewers', { method: 'POST', body: { type: 'offer', sdp: pc.localDescription.sdp,
        mode: requestedMode(), codec: fallback('compatCodec') ? 'vp8' : null, client: clientKind() } });
      if (superseded()) { api('/api/viewers/' + answer.viewer_id, { method: 'DELETE' }).catch(() => {}); return; }
      viewer.id = answer.viewer_id; viewer.mode = answer.mode; viewer.codec = answer.codec;
      await pc.setRemoteDescription({ type: answer.type, sdp: answer.sdp });
      viewer.last = null; viewer.reported = null; viewer.audioDropouts = 0; viewer.decodedEver = false;
      viewer.watchdog = { since: performance.now(), decoded: 0, packets: 0, asked: false };
      startStats();
    } catch (error) {
      if (superseded()) return;
      if (error.status === 409) scheduleReconnect(error.message, 10000);
      else scheduleReconnect('Could not connect: ' + error.message);
    } finally {
      if (generation === viewer.generation) viewer.connecting = false;
    }
  }

  async function disconnect() {
    viewer.generation++;  // supersedes any in-flight connect()
    viewer.connecting = false;
    clearTimeout(viewer.retryTimer); viewer.retryTimer = null;
    await teardown();
  }

  async function teardown() {
    clearInterval(viewer.statsTimer);
    const pc = viewer.pc, id = viewer.id;
    viewer.pc = null; viewer.id = null; viewer.mode = null; viewer.watchdog = null;
    if (pc) pc.close();
    if (video.srcObject) { video.srcObject = null; }
    $('tap').hidden = true;
    if (id) await api('/api/viewers/' + id, { method: 'DELETE', keepalive: true }).catch(() => {});
    $('stats').textContent = '';
  }

  function scheduleReconnect(reason, delay) {
    if (!viewer.wanted || !hasMedia(status)) return;
    clearTimeout(viewer.retryTimer);
    const wait = delay || Math.min(1000 * Math.pow(2, viewer.retry), 8000);
    viewer.retry++;
    toast(reason + ' — retrying in ' + Math.round(wait / 1000) + ' s', wait);
    viewer.retryTimer = setTimeout(() => { viewer.retryTimer = null; if (hasMedia(status)) connect(); }, wait);
  }

  // Join while the phone is connected; leave shortly after it stops (not at once: a new phone
  // taking over, or a brief hiccup, must not drop and rebuild the connection).
  function syncViewer(s) {
    const media = hasMedia(s);
    if (media) {
      clearTimeout(viewer.idleTimer); viewer.idleTimer = null;
      if (viewer.wanted && !viewer.pc && !viewer.connecting) connect();
    } else if ((viewer.pc || viewer.connecting) && !viewer.idleTimer) {
      viewer.idleTimer = setTimeout(() => {
        viewer.idleTimer = null;
        if (hasMedia(status)) return;
        viewer.sessionCompat = false;   // the next phone starts with its own stream again
        viewer.compatTried = false;
        disconnect();
      }, IDLE_DISCONNECT_MS);
    }
    stage.classList.toggle('live', media);
    if (!media) $('tap').hidden = true;
  }

  function play() {
    const attempt = video.play();
    if (attempt && attempt.catch) {
      attempt.then(() => { $('tap').hidden = true; }).catch((error) => {
        // Only a blocked autoplay needs a tap. An interrupted play (the stream changed, or no
        // picture has arrived yet) does not.
        if (error && error.name === 'NotAllowedError' && stage.classList.contains('live')) $('tap').hidden = false;
      });
    }
  }

  function updateSoundButton() {
    const button = $('btn-sound');
    button.querySelector('use').setAttribute('href', video.muted ? '#i-mute' : '#i-sound');
    button.querySelector('span').textContent = video.muted ? 'Audio off' : 'Audio on';
    button.title = video.muted ? 'Turn audio on' : 'Turn audio off';
    button.classList.toggle('attention', video.muted);   // browsers start muted: invite the click
    button.classList.toggle('on', !video.muted);
  }

  function switchToCompat(remember) {
    if (remember) {
      store.set('forceCompat', Date.now());
      toast('This browser could not play the device\'s own video; switching to compatibility mode.', 6000);
    } else {
      viewer.sessionCompat = true;   // until the phone stops: the server re-encodes the current picture
      toast('Showing the current picture re-encoded (the device sends its own video only now and then).', 5000);
    }
    connect();
  }

  // ---------------------------------------------------------------- stats + watchdog
  function startStats() {
    clearInterval(viewer.statsTimer);
    viewer.statsTimer = setInterval(collectStats, 1000);
  }

  async function collectStats() {
    const pc = viewer.pc;
    if (!pc) return;
    let report;
    try { report = await pc.getStats(); } catch (e) { return; }
    if (pc !== viewer.pc) return;
    const now = performance.now();
    const s = { t: now };
    const codecs = {};
    report.forEach((r) => { if (r.type === 'codec') codecs[r.id] = r; });
    report.forEach((r) => {
      if (r.type === 'inbound-rtp' && r.kind === 'video') {
        Object.assign(s, { vbytes: r.bytesReceived, vpackets: r.packetsReceived, decoded: r.framesDecoded,
          fps: r.framesPerSecond, w: r.frameWidth, h: r.frameHeight, lost: r.packetsLost, freezes: r.freezeCount,
          vjb: r.jitterBufferDelay, vjbn: r.jitterBufferEmittedCount, dropped: r.framesDropped,
          codec: codecs[r.codecId] ? codecs[r.codecId].mimeType.replace('video/', '') : '' });
      }
      if (r.type === 'inbound-rtp' && r.kind === 'audio') {
        Object.assign(s, { ajb: r.jitterBufferDelay, ajbn: r.jitterBufferEmittedCount,
          aconcealed: r.concealedSamples, asamples: r.totalSamplesReceived, aevents: r.concealmentEvents,
          adiscarded: r.packetsDiscarded, apackets: r.packetsReceived });
      }
      if (r.type === 'candidate-pair' && (r.nominated || r.selected) && r.state === 'succeeded') {
        if (Number.isFinite(r.currentRoundTripTime)) s.rtt = r.currentRoundTripTime * 1000;
        const local = report.get(r.localCandidateId);
        if (local) s.relay = local.candidateType === 'relay';
      }
    });
    const prev = viewer.last;
    viewer.last = s;
    const d = (a, b) => (Number.isFinite(a) && Number.isFinite(b) ? a - b : NaN);
    if (prev) {
      s.kbps = d(s.vbytes, prev.vbytes) * 8 / d(s.t, prev.t);
      const n = d(s.vjbn, prev.vjbn);
      s.vjbms = n > 0 ? d(s.vjb, prev.vjb) / n * 1000 : NaN;
      const an = d(s.ajbn, prev.ajbn);
      s.ajbms = an > 0 ? d(s.ajb, prev.ajb) / an * 1000 : NaN;
      // Share of the last second's audio the browser had to invent (late, lost or discarded packets).
      const played = d(s.asamples, prev.asamples);
      s.aconcealedPct = played > 0 ? Math.min(100, Math.max(0, d(s.aconcealed, prev.aconcealed) / played * 100)) : NaN;
      // A dropout: invented audio while packets kept arriving (not the phone simply going quiet).
      if (s.aevents > prev.aevents && s.apackets - prev.apackets >= 25) viewer.audioDropouts++;
    }
    watchdog(s);
    renderStats(s);
    if (Math.floor(now / 5000) !== Math.floor((prev ? prev.t : 0) / 5000)) reportStats(s);
  }

  function watchdog(s) {
    const w = viewer.watchdog;
    if (!w || !viewer.pc || viewer.pc.connectionState !== 'connected') return;
    const now = s.t;
    if (Number.isFinite(s.decoded) && s.decoded > w.decoded) {
      w.decoded = s.decoded; w.since = now; w.asked = false; w.packets = s.vpackets || 0; viewer.decodedEver = true;
      return;
    }
    // A still phone screen sends nothing: not a stall. Stalled: packets arrive but never decode,
    // or the phone's picture changes (the server has new video) while this browser shows none of it.
    const arriving = Number.isFinite(s.vpackets) && s.vpackets > w.packets + 20;
    const fresh = !!status && status.source.state === 'streaming';
    if (!arriving && !fresh) { w.since = now; return; }
    const stuck = now - w.since;
    const auto = $('mode').value === 'auto';
    if (stuck > 3000 && !w.asked && viewer.id) {
      w.asked = true;
      const id = viewer.id;
      api('/api/viewers/' + id + '/keyframe', { method: 'POST' }).then((r) => {
        // The server knows this viewer waits for a keyframe the phone may not send for minutes.
        if (r && r.stuck && id === viewer.id && viewer.mode === 'passthrough' && auto && !viewer.compatTried) {
          viewer.compatTried = true;
          switchToCompat(false);
        }
      }).catch(() => {});
    }
    if (stuck > 9000 && viewer.mode === 'passthrough' && auto && !viewer.compatTried) {
      viewer.compatTried = true;
      // Remembered for a week only when the phone's own stream arrived and never played:
      // then this browser most likely cannot decode it.
      switchToCompat(arriving && !viewer.decodedEver);
      return;
    }
    // Last resort: baseline H.264 never decoded either (e.g. Firefox on Linux without
    // OpenH264) -> VP8, which every WebRTC browser must support.
    if (stuck > 9000 && arriving && !viewer.decodedEver && viewer.mode === 'compat' && /^H264/.test(viewer.codec || '') && !fallback('compatCodec')) {
      store.set('compatCodec', Date.now());
      toast('H.264 is not playing in this browser; switching to VP8.', 6000);
      connect();
    }
  }

  function fmt(v, digits, unit) { return Number.isFinite(v) ? v.toFixed(digits) + (unit || '') : '–'; }

  function renderStats(s) {
    if ($('stats').hidden) return;
    $('stats').textContent = [
      'input    ' + ({ usb: 'USB cable', android: 'Android' }[status && status.source.input] || 'AirPlay'),
      'mode     ' + (viewer.mode || '–') + '  ' + (viewer.codec || s.codec || ''),
      'video    ' + (s.w ? s.w + '×' + s.h : '–') + ' @ ' + fmt(s.fps, 0, ' fps'),
      'bitrate  ' + fmt(s.kbps, 0, ' kbps'),
      'buffer   video ' + fmt(s.vjbms, 0, ' ms') + '  audio ' + fmt(s.ajbms, 0, ' ms'),
      'audio    ' + fmt(s.aconcealedPct, 1, '% filled in') + ', ' + viewer.audioDropouts + ' dropouts',
      'network  rtt ' + fmt(s.rtt, 0, ' ms') + '  lost ' + (Number.isFinite(s.lost) ? s.lost : '–') +
        (s.relay === undefined ? '' : s.relay ? '  through the page\'s connection' : '  direct'),
      'frames   ' + (s.decoded || 0) + ' decoded, ' + (s.dropped || 0) + ' dropped, ' + (s.freezes || 0) + ' freezes',
    ].join('\n');
  }

  function reportStats(s) {
    if (!viewer.id) return;
    const body = {};
    const put = (k, v) => { if (Number.isFinite(v)) body[k] = Math.round(v * 1000) / 1000; };
    put('video_width', s.w); put('video_height', s.h); put('video_fps', s.fps); put('video_bitrate_kbps', s.kbps);
    put('video_lost', s.lost); put('video_freeze_count', s.freezes); put('video_jitter_buffer_ms', s.vjbms);
    put('audio_jitter_buffer_ms', s.ajbms); put('browser_rtt_ms', s.rtt); put('video_frames_decoded', s.decoded);
    put('video_dropped', s.dropped);
    // Concealed share over the whole reporting window (5 s), not just the last second.
    const r = viewer.reported;
    if (r && Number.isFinite(s.asamples) && s.asamples > r.asamples) {
      put('audio_concealed_pct', Math.min(100, Math.max(0, (s.aconcealed - r.aconcealed) / (s.asamples - r.asamples) * 100)));
    }
    viewer.reported = { asamples: s.asamples, aconcealed: s.aconcealed };
    put('audio_concealment_events', s.aevents); put('audio_dropouts', viewer.audioDropouts);
    put('audio_packets_discarded', s.adiscarded); put('audio_packets_received', s.apackets);
    if (viewer.mode) body.viewer_mode = viewer.mode;
    if (s.relay !== undefined) body.connection_kind = s.relay ? 'relay' : 'direct';
    body.paused = video.paused; body.muted = video.muted; body.document_hidden = document.hidden;
    api('/api/viewers/' + viewer.id + '/stats', { method: 'POST', body }).catch(() => {});
  }

  // ---------------------------------------------------------------- status
  const receiverText = { running: 'Ready', checking: 'Starting', restarting: 'Restarting', stopping: 'Stopping',
    stopped: 'Stopped', failed: 'Problem', unavailable: 'Not available' };
  const receiverTone = { running: 'ok', checking: 'warn', restarting: 'warn', stopping: 'warn', stopped: 'muted', failed: 'bad', unavailable: 'bad' };
  const usbText = { streaming: 'Mirroring', connecting: 'Connecting', pairing: 'Waiting for Trust', waiting: 'Not connected',
    driver: 'Driver needed', held: 'Disconnected', error: 'Problem', unavailable: 'Not available', off: 'Off' };
  const usbTone = { streaming: 'ok', connecting: 'warn', pairing: 'warn', waiting: 'muted', driver: 'warn', held: 'muted', error: 'bad',
    unavailable: 'muted', off: 'muted' };
  let status = null, lastMedia = false;
  // 'owner' (this computer, or signed in with the owner code) or 'viewer' (viewer code:
  // watch only). Owner pages stay hidden until the server has said which one this is.
  let role = null, startPage = 'mirror';

  function applyRole(next) {
    if (next === role) return;
    const first = role === null;
    role = next;
    document.body.dataset.role = role;
    if (role === 'owner' && first) selectPage(startPage);
    if (role !== 'owner') selectPage('mirror', false);
  }

  async function poll() {
    try {
      status = await api('/api/status', { timeout: 4000 });
      applyRole(status.client.role === 'viewer' ? 'viewer' : 'owner');
      if (status.quitting && !appOff) { showQuit(); return; }   // quit from its icon or another window
      // After Quit the server answers for a moment while it shuts down: only a
      // reply after it was seen gone means MirrorDesk was started again.
      if (appOff === 'quitting' && Date.now() - quitAt < 15000) return;
      if (appOff) {  // started again (or the quit never happened): pick up where we left off
        appOff = false; $('idle-steps').hidden = false; $('idle-sub').hidden = false; toast('');
        loadDesktop();
      }
      renderStatus(status);
    } catch (e) {
      if (appOff) appOff = 'off';
      setChip('chip-phone', appOff ? '–' : 'Offline', appOff ? 'muted' : 'bad');
      setChip('chip-receiver', appOff ? 'Off' : 'Offline', appOff ? 'muted' : 'bad');
    } finally {
      setTimeout(poll, document.hidden ? 4000 : 1000);
    }
  }

  function describeSource(src) {
    if (src.state === 'streaming') return (src.width ? src.width + '×' + src.height : 'video') + ' at ' + Math.round(src.fps) + ' fps, ' + src.bitrate_kbps + ' kbps' + (src.profile ? ', H.264 ' + src.profile : '');
    if (src.state === 'paused') return 'Screen unchanged' + (src.width ? ' (' + src.width + '×' + src.height + ')' : '');
    if (src.state === 'audio_only') return 'Audio only';
    return 'No video';
  }

  function phoneSummary(s) {
    const src = s.source, rx = s.receiver;
    const usb = s.usb || {}, android = s.android || {};
    if (src.state === 'audio_only') return ['Audio only', 'ok'];
    if (hasMedia(s)) {
      const how = src.input === 'usb' ? 'USB' : src.input === 'android' ? 'Android' : 'Wi-Fi';
      return ['Mirroring · ' + how, 'ok'];
    }
    if (usb.state === 'pairing') return ['Tap Trust on the iPhone', 'warn'];
    if (android.state === 'unauthorized') return ['Tap Allow on the phone', 'warn'];
    if (rx.phone === 'connecting' || usb.state === 'connecting' || android.state === 'connecting') return ['Connecting…', 'warn'];
    return ['Not connected', 'muted'];
  }

  // MirrorDesk was updated while this page stayed open: load the new page (an old page kept its
  // old script, which joined as a viewer with no phone and could not drive the new page).
  const pageBuild = (document.querySelector('meta[name="mirrordesk-build"]') || {}).content;
  function reloadIfUpdated(s) {
    if (!pageBuild || !s.build || s.build === pageBuild) return false;
    try {
      if (sessionStorage.getItem('mirrordesk.reloadedFor') === s.build) return false;   // once per update
      sessionStorage.setItem('mirrordesk.reloadedFor', s.build);
    } catch (e) { /* storage blocked: reload anyway */ }
    location.reload();
    return true;
  }

  function renderStatus(s) {
    if (reloadIfUpdated(s)) return;
    const src = s.source;
    const media = hasMedia(s);
    const [phoneText, phoneTone] = phoneSummary(s);
    setChip('chip-phone', phoneText, phoneTone);
    if (lastMedia && !media) toast(role === 'viewer' ? 'The device stopped sharing its screen.' : 'The device stopped mirroring.', 4000);
    lastMedia = media;
    $('nav-version').textContent = 'Version ' + s.version;
    $('nav-live').hidden = !media;
    // The picture stays up while the phone mirrors, also when its screen is unchanged.
    $('idle').hidden = ['streaming', 'paused'].includes(src.state);
    if (role === 'viewer') {
      // Watch only: no pairing code, phone names or controls; the status has none of them.
      $('idle-title').textContent = src.state === 'audio_only' ? 'Playing audio from the device' : 'Waiting for a device';
      $('idle-sub').textContent = 'The screen appears here as soon as the device starts sharing it.';
      $('idle-hint').textContent = '';
      setPhoneControl(false);
    } else {
      renderOwner(s);
    }
    syncViewer(s);
  }

  function renderOwner(s) {
    const rx = s.receiver, src = s.source;
    const usb = s.usb || { state: 'unavailable', enabled: false, message: '' };
    const android = s.android || { state: 'unavailable', enabled: false, message: '' };
    setChip('chip-receiver', receiverText[rx.state] || rx.state, receiverTone[rx.state] || 'muted');
    renderWatching(s);
    // Idle screen: the three ways to connect, each with its state.
    $('idle-name').textContent = rx.name;
    $('idle-pin').textContent = rx.pin || '····';
    const wirelessDown = rx.state === 'failed' || rx.state === 'unavailable';
    const cableReady = (usb.enabled && usb.state !== 'unavailable') || (android.enabled && android.state !== 'unavailable');
    $('idle-title').textContent = src.state === 'audio_only' ? 'Playing audio from your phone'
      : usb.state === 'connecting' || android.state === 'connecting' ? 'Connecting to the device…'
      : usb.state === 'pairing' ? 'Tap Trust on the iPhone'
      : android.state === 'unauthorized' ? 'Tap Allow on the Android phone'
      : rx.state === 'running' || (wirelessDown && cableReady) ? 'Ready to mirror'
      : wirelessDown ? 'Wi-Fi mirroring is not available' : 'Starting…';
    $('idle-sub').textContent = 'Connect a device in one of these ways.';
    $('idle-hint').textContent = rx.state === 'running' ? 'For Wi-Fi, the device and this computer must be on the same network.' : rx.message;
    setBadge('way-airplay-dot', receiverText[rx.state] || rx.state, receiverTone[rx.state] || 'muted');
    $('way-airplay').classList.toggle('off', rx.state !== 'running');
    const usbOff = !usb.enabled && usb.state !== 'unavailable';
    $('idle-usb').textContent = usbOff ? 'Turned off in Settings.'
      : ['driver', 'error', 'unavailable', 'pairing', 'connecting', 'held'].includes(usb.state) ? usb.message
      : 'Plug the iPhone into this computer and unlock it.';
    setBadge('way-usb-dot', usbOff ? 'Off' : (usbText[usb.state] || usb.state), usbOff ? 'muted' : (usbTone[usb.state] || 'muted'));
    const androidOff = !android.enabled && android.state !== 'unavailable';
    $('idle-android').textContent = androidOff ? 'Turned off in Settings.'
      : ['unauthorized', 'error', 'unavailable', 'installing', 'held'].includes(android.state) ? android.message
      : 'Turn on USB debugging once, then plug the phone into this computer.';
    setBadge('way-android-dot', androidOff ? 'Off' : (androidText[android.state] || android.state),
             androidOff ? 'muted' : (androidTone[android.state] || 'muted'));
    renderPhonesPage(s, usb, android);
    renderDeveloper(s, usb, android);
  }

  // ---------------------------------------------------------------- phones page
  function renderPhonesPage(s, usb, android) {
    const rx = s.receiver, src = s.source;
    // What mirrors right now, with Disconnect.
    const media = hasMedia(s);
    $('now-card').hidden = !media;
    if (media) {
      let name = 'Device', detail;
      if (src.input === 'usb') { name = (usb.device && usb.device.name) || 'iPhone'; detail = 'USB cable'; }
      else if (src.input === 'android') {
        name = (android.device && android.device.name) || 'Android phone';
        detail = android.device && android.device.transport === 'wifi' ? 'Android · Wi-Fi' : 'Android · USB cable';
      } else { name = rx.phone_name || 'iPhone or iPad'; detail = 'Wi-Fi (AirPlay)'; }
      if (src.state === 'paused') detail += ' · screen unchanged';
      else if (src.state === 'audio_only') detail += ' · audio only';
      else if (src.width) detail += ' · ' + src.width + ' × ' + src.height;
      $('now-title').textContent = name;
      $('now-detail').textContent = detail;
      $('now-icon').setAttribute('href', src.input === 'android' ? '#i-android' : '#i-phone');
    }
    setBadge('rx-state', receiverText[rx.state] || rx.state, receiverTone[rx.state] || 'muted');
    $('rx-message').textContent = rx.message;
    $('rx-name').textContent = rx.name;
    $('rx-pin').textContent = rx.pin || '····';
    const word = { mirroring: 'mirroring', paused: 'mirroring, screen unchanged', audio_only: 'audio only', connecting: 'connecting…' }[rx.phone];
    $('rx-phone').textContent = !word ? 'None connected'
      : rx.phone_name ? rx.phone_name + (rx.phone_model ? ' (' + rx.phone_model + ')' : '') + ' · ' + word
      : word.charAt(0).toUpperCase() + word.slice(1);
    $('rx-start').disabled = ['running', 'checking', 'restarting'].includes(rx.state);
    $('rx-stop').disabled = ['stopped', 'unavailable'].includes(rx.state);
    const disc = $('rx-discovery');
    disc.hidden = !rx.discovery_error;
    disc.textContent = rx.discovery_error ? 'Devices may not find this computer: ' + rx.discovery_error + '. Check Bonjour and the firewall.' : '';
    renderUsb(usb);
    renderAndroid(android);
  }

  function selectPhoneTab(kind, remember) {
    for (const [name, tab, panel] of [['iphone', 'tab-iphone', 'phones-iphone'], ['android', 'tab-android', 'phones-android']]) {
      $(tab).setAttribute('aria-selected', String(name === kind));
      $(panel).hidden = name !== kind;
    }
    if (remember !== false) store.set('phoneTab', kind);
  }

  function renderUsb(usb) {
    const off = !usb.enabled && usb.state !== 'unavailable';
    setBadge('usb-state', off ? 'Off' : (usbText[usb.state] || usb.state), off ? 'muted' : (usbTone[usb.state] || 'muted'));
    $('usb-message').textContent = off ? 'Cable mirroring is turned off. Turn it on in Settings.' : usb.message;
    const device = usb.device;
    $('usb-facts').hidden = !device;
    if (device) $('usb-device').textContent = device.name + (device.width ? ', ' + device.width + '×' + device.height : '');
    $('btn-usb-reconnect').disabled = !usb.enabled || usb.state === 'unavailable';
    // Windows cable driver: only on the computer itself (the permission prompt appears there).
    const local = status && status.client && status.client.local;
    const job = usb.driver_job || {};
    const canDrive = usb.driver_tool && local;
    $('btn-usb-driver').hidden = !(canDrive && usb.state === 'driver');
    $('btn-usb-driver').disabled = job.state === 'running';
    $('btn-usb-restore').hidden = !canDrive;
    $('usb-driver-msg').hidden = !job.message;
    $('usb-driver-msg').textContent = job.message || '';
    $('usb-driver-help').hidden = !(status && status.platform && status.platform.os === 'Windows');
  }

  async function usbDriver(action) {
    if (action === 'driver-restore' && !confirm("Give the iPhone back to Apple's driver? Cable mirroring then stops working until the cable driver is installed again.")) return;
    try { await api('/api/usb/' + action, { method: 'POST' }); toast('Windows asks for permission now; allow it to continue.', 8000); }
    catch (e) { toast(e.message, 8000); }
  }

  async function disconnectPhone() {
    try { await api('/api/phone/disconnect', { method: 'POST', timeout: 30000 }); toast('The device was disconnected.', 4000); }
    catch (e) { toast(e.message, 6000); }
  }

  // ---------------------------------------------------------------- android
  const androidText = { streaming: 'Mirroring', connecting: 'Connecting', waiting: 'Not connected', unauthorized: 'Allow on phone',
    installing: 'Installing', unavailable: 'Not set up', held: 'Disconnected', error: 'Problem', off: 'Off' };
  const androidTone = { streaming: 'ok', connecting: 'warn', waiting: 'muted', unauthorized: 'warn', installing: 'warn',
    unavailable: 'muted', held: 'muted', error: 'bad', off: 'muted' };
  const audioText = { on: 'On', starting: 'Starting…', off: 'Off in Settings', unsupported: 'Not available', error: 'Not available' };

  function renderAndroid(a) {
    const off = !a.enabled && a.state !== 'unavailable';
    setBadge('android-state', off ? 'Off' : (androidText[a.state] || a.state), off ? 'muted' : (androidTone[a.state] || 'muted'));
    $('android-message').textContent = off ? 'Android mirroring is turned off. Turn it on in Settings.' : a.message;
    const tools = a.tools || {};
    const install = tools.install || {};
    $('android-install').hidden = !(tools.installable && !tools.adb) && install.state !== 'running';
    $('btn-android-install').disabled = install.state === 'running';
    $('android-install-msg').textContent = install.state === 'running' ? 'Downloading… ' + Math.round((install.progress || 0) * 100) + '%'
      : install.state === 'failed' ? install.message : "Google's adb, about 8 MB.";
    const device = a.device;
    $('android-facts').hidden = !device;
    if (device) {
      $('android-device').textContent = device.name + (device.android ? ', Android ' + device.android : '') +
        (device.width ? ', ' + device.width + '×' + device.height : '');
      $('android-transport').textContent = device.transport === 'wifi' ? 'Wi-Fi' : 'USB cable';
      const audio = a.audio || {};
      $('android-audio').textContent = audio.message || audioText[audio.state] || '–';
      $('android-control').textContent = a.control_note || (a.control ? 'Click, drag and type on the picture'
        : a.state === 'streaming' ? 'Not allowed from this device (Settings)' : '–');
    }
    const devices = a.devices || [];
    const ready = devices.filter((d) => d.state === 'device');
    $('android-pick-row').hidden = ready.length < 2;
    const pick = $('android-pick');
    if (ready.length >= 2 && document.activeElement !== pick) {
      pick.textContent = '';
      for (const d of ready) {
        const o = document.createElement('option');
        o.value = d.serial; o.textContent = d.name + (d.transport === 'wifi' ? ' (Wi-Fi)' : ' (USB)');
        o.selected = d.active;
        pick.appendChild(o);
      }
    }
    const active = devices.find((d) => d.active);
    $('btn-android-disconnect').hidden = !(active && active.transport === 'wifi');
    $('btn-android-reconnect').disabled = !a.enabled || !tools.adb;
    if (a.pairing && a.pairing.message) $('android-pair-msg').textContent = a.pairing.message;
    if (a.pairing && ['connected', 'failed'].includes(a.pairing.state)) $('android-qr').hidden = true;
    setPhoneControl(!!a.control);
  }

  async function androidPost(action, body) {
    try { return await api('/api/android/' + action, { method: 'POST', body: body || {}, timeout: 40000 }); }
    catch (e) { toast(e.message, 8000); $('android-pair-msg').textContent = e.message; return null; }
  }

  let qrTimer;
  async function showPairingQr() {
    const r = await androidPost('qr');
    if (!r) return;
    $('android-qr-img').src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(r.qr_svg);
    $('android-qr').hidden = false;
    $('android-pair-msg').textContent = 'Scan the code with the phone (Pair device with QR code).';
    clearTimeout(qrTimer);
    qrTimer = setTimeout(() => { $('android-qr').hidden = true; }, (r.expires_in || 120) * 1000);
  }

  async function submitPairing(event) {
    event.preventDefault();
    const form = $('android-pair-form');
    const action = (event.submitter && event.submitter.value) || 'pair';
    const port = parseInt(form.elements.port.value, 10);
    const body = { host: form.elements.host.value.trim(), port: Number.isFinite(port) ? port : 0 };
    if (action === 'pair') body.code = form.elements.code.value.trim();
    $('android-pair-msg').textContent = action === 'pair' ? 'Pairing…' : 'Connecting…';
    const r = await androidPost(action, body);
    if (r) { $('android-pair-msg').textContent = r.message || (r.pairing && r.pairing.message) || 'Done.'; form.elements.code.value = ''; }
  }

  async function loadAndroidLog() {
    try { const data = await api('/api/android/log'); $('android-log').textContent = data.lines.map((l) => new Date(l.t * 1000).toLocaleTimeString() + '  ' + l.line).join('\n') || '(no messages yet)'; }
    catch (e) { $('android-log').textContent = e.message; }
  }

  // ---------------------------------------------------------------- phone control (Android)
  // Pointer and keyboard input on the picture go to the phone over a WebSocket
  // (same-origin only). Coordinates are sent as fractions of the picture, so the
  // server maps them to the phone's current screen size, letterboxing aside.
  const control = { on: false, ws: null, retry: null, pointers: new Map(), last: new Map() };

  function setPhoneControl(on) {
    if (on === control.on) return;
    control.on = on;
    stage.classList.toggle('controllable', on);
    $('phone-keys').hidden = !on;
    if (on) { stage.tabIndex = 0; openControl(); }
    else { stage.removeAttribute('tabindex'); closeControl(); }
  }

  function openControl() {
    clearTimeout(control.retry);
    if (!control.on || (control.ws && control.ws.readyState <= 1)) return;
    const ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/api/android/control');
    control.ws = ws;
    ws.onclose = () => { if (control.ws === ws) { control.ws = null; if (control.on) control.retry = setTimeout(openControl, 2000); } };
  }

  function closeControl() {
    clearTimeout(control.retry);
    const ws = control.ws;
    control.ws = null;
    if (ws) ws.close();
    control.pointers.clear();
  }

  function sendControl(event) {
    const ws = control.ws;
    if (ws && ws.readyState === 1) ws.send(JSON.stringify(event));
  }

  function picturePoint(event, clamp) {
    const rect = video.getBoundingClientRect();
    const vw = video.videoWidth, vh = video.videoHeight;
    if (!vw || !vh) return null;
    const scale = Math.min(rect.width / vw, rect.height / vh);
    const w = vw * scale, h = vh * scale;
    let x = (event.clientX - rect.left - (rect.width - w) / 2) / w;
    let y = (event.clientY - rect.top - (rect.height - h) / 2) / h;
    if (!clamp && (x < 0 || x > 1 || y < 0 || y > 1)) return null;
    x = Math.min(1, Math.max(0, x)); y = Math.min(1, Math.max(0, y));
    return { x: Math.round(x * 10000) / 10000, y: Math.round(y * 10000) / 10000 };
  }

  function pointerId(event) {
    if (event.pointerType === 'mouse' || event.pointerType === 'pen') return -2;   // one virtual finger
    if (!control.pointers.has(event.pointerId)) {
      const used = new Set(control.pointers.values());
      let id = 0;
      while (used.has(id) && id < 9) id++;
      control.pointers.set(event.pointerId, id);
    }
    return control.pointers.get(event.pointerId);
  }

  function onPointer(event) {
    if (!control.on || (event.pointerType === 'mouse' && event.button > 0 && event.type === 'pointerdown')) return;
    const down = event.type === 'pointerdown';
    const pressed = down || control.last.has(event.pointerId);
    if (!pressed) return;
    const point = picturePoint(event, !down);
    if (!point) return;
    event.preventDefault();
    const id = pointerId(event);
    if (down) {
      try { video.setPointerCapture(event.pointerId); } catch (e) { /* not capturable */ }
      stage.focus({ preventScroll: true });
      control.last.set(event.pointerId, point);
      sendControl({ t: 'touch', a: 'down', id, x: point.x, y: point.y, p: event.pressure || 1 });
    } else if (event.type === 'pointermove') {
      const last = control.last.get(event.pointerId);
      if (last && last.x === point.x && last.y === point.y) return;
      control.last.set(event.pointerId, point);
      sendControl({ t: 'touch', a: 'move', id, x: point.x, y: point.y, p: event.pressure || 1 });
    } else {
      control.last.delete(event.pointerId);
      control.pointers.delete(event.pointerId);
      sendControl({ t: 'touch', a: event.type === 'pointercancel' ? 'cancel' : 'up', id, x: point.x, y: point.y });
    }
  }

  function onWheel(event) {
    if (!control.on) return;
    const point = picturePoint(event, false);
    if (!point) return;
    event.preventDefault();
    const unit = event.deltaMode === 1 ? 1 : 1 / 100;   // lines or pixels -> scroll steps
    sendControl({ t: 'scroll', x: point.x, y: point.y, dx: -event.deltaX * unit, dy: -event.deltaY * unit });
  }

  const KEY_NAMES = { Enter: 'enter', Backspace: 'backspace', Delete: 'delete', Tab: 'tab', Escape: 'back',
    ArrowLeft: 'left', ArrowRight: 'right', ArrowUp: 'up', ArrowDown: 'down', Home: 'home' };
  function onKey(event) {
    if (!control.on || event.target !== stage || event.ctrlKey || event.metaKey || event.altKey) return;
    if (KEY_NAMES[event.key]) { event.preventDefault(); event.stopPropagation(); sendControl({ t: 'key', k: KEY_NAMES[event.key] }); return; }
    if (event.key.length === 1) { event.preventDefault(); event.stopPropagation(); sendControl({ t: 'text', s: event.key }); }
  }

  function onPaste(event) {
    if (!control.on || event.target !== stage) return;
    const text = (event.clipboardData && event.clipboardData.getData('text')) || '';
    event.preventDefault();
    for (let i = 0; i < text.length; i += 200) sendControl({ t: 'text', s: text.slice(i, i + 200) });
  }

  // ---------------------------------------------------------------- pages
  const PAGES = ['mirror', 'connect', 'share', 'settings', 'developer'];
  function selectPage(name, remember) {
    if (!PAGES.includes(name) || (name !== 'mirror' && !$('page-' + name)) || role !== 'owner') name = 'mirror';
    for (const item of document.querySelectorAll('.nav-item')) {
      if (item.dataset.page === name) item.setAttribute('aria-current', 'page'); else item.removeAttribute('aria-current');
    }
    const open = name !== 'mirror';
    $('panel').hidden = !open;
    $('workspace').dataset.panel = open ? 'open' : 'none';
    for (const page of document.querySelectorAll('.page')) page.hidden = page.id !== 'page-' + name;
    if (open) $('panel-title').textContent = $('page-' + name).dataset.title;
    $('view-title').textContent = open ? $('page-' + name).dataset.title : 'Screen';
    if (remember !== false) store.set('page', name);
    if (name === 'share') { loadNetwork(); if (status) renderWatching(status); }
    if (name === 'settings') { loadSettings(); loadAccess().catch((e) => toast('Could not load the codes: ' + e.message)); }
    // The Phones page opens on the tab of the phone that mirrors (else the one last chosen).
    if (name === 'connect' && hasMedia(status)) selectPhoneTab(status.source.input === 'android' ? 'android' : 'iphone', false);
  }

  // ---------------------------------------------------------------- viewers (chip + Viewers page)
  // Viewers are the other devices watching; this browser is not one of them.
  function renderWatching(s) {
    const mine = viewer.id ? viewer.id.slice(0, 8) : null;
    const others = (s.viewers.list || []).filter((v) => !['failed', 'closed'].includes(v.state) && v.id !== mine);
    const full = s.viewers.count >= s.viewers.max;
    const count = others.length + (others.length === 1 ? ' viewer' : ' viewers');
    setChip('chip-viewers', full ? 'Full (' + s.viewers.max + ')' : count, full ? 'warn' : others.length ? 'ok' : 'muted');
    $('viewers-short').textContent = full ? 'Full' : String(others.length);
    $('chip-viewers').title = 'Devices watching besides this one. At most ' + s.viewers.max + ' at once (Settings).';
    if ($('page-share').hidden) return;
    setBadge('watch-count', String(others.length), others.length ? 'ok' : 'muted');
    const ul = $('watchers');
    ul.textContent = '';
    if (!others.length) { ul.appendChild(el('li', 'empty', 'No viewers')); return; }
    for (const v of others) {
      const li = el('li');
      const badge = el('span', 'icon'); badge.appendChild(icon(v.local ? 'mirror' : 'user'));
      const who = el('div', 'who');
      who.appendChild(el('b', '', v.local ? 'This computer' : v.role === 'owner' ? 'Owner device' : 'Viewer'));
      const since = v.age_s < 90 ? v.age_s + ' s' : Math.round(v.age_s / 60) + ' min';
      who.appendChild(el('span', '', (v.local ? 'On this computer' : 'Over the network') + ' · ' +
        ({ passthrough: 'Original video', compat: 'Re-encoded video' }[v.mode] || 'Starting') + ' · ' + since));
      const state = el('span', 'badge', v.state === 'connected' ? 'Watching' : 'Connecting');
      state.dataset.tone = v.state === 'connected' ? 'ok' : 'warn';
      li.append(badge, who, state);
      ul.appendChild(li);
    }
  }

  // ---------------------------------------------------------------- settings
  // Readable names for setting values (the API keeps its stable identifiers).
  const LABELS = {
    resolution: { '720p': '720p (1280 × 720)', '1080p': '1080p (1920 × 1080)', '1440p': '1440p (2560 × 1440), experimental' },
    fps: { 30: '30 frames per second', 60: '60 frames per second' },
    video_mode: { auto: 'Automatic', passthrough: "The phone's own video", compat: 'Compatibility (re-encoded)' },
    compat_max_height: { 480: '480p', 720: '720p', 1080: '1080p' },
    av_sync: { low_latency: 'Lowest delay', synced: "Follow the device's timing (more delay)" },
    android_max_size: { 0: "The phone's own size", 2560: '2560 pixels', 1920: '1920 pixels (recommended)', 1600: '1600 pixels', 1280: '1280 pixels', 1024: '1024 pixels' },
    android_bit_rate: { 2: '2 Mbit/s (slow Wi-Fi)', 4: '4 Mbit/s', 8: '8 Mbit/s (recommended)', 12: '12 Mbit/s', 16: '16 Mbit/s', 24: '24 Mbit/s (cable)' },
    android_fps: { 30: '30 frames per second', 60: '60 frames per second' },
    android_control: { off: 'Nobody (watch only)', local: 'This computer only', all: 'This computer and owner devices' },
  };

  // Settings sections: the form's fields and the cards that belong to the same section.
  const SETTINGS_TABS = ['general', 'video', 'phones', 'access'];
  function selectSettingsTab(name, remember) {
    if (!SETTINGS_TABS.includes(name)) name = 'general';
    for (const tab of SETTINGS_TABS) {
      $('stab-' + tab).setAttribute('aria-selected', String(tab === name));
      $('sset-' + tab).hidden = tab !== name;
    }
    for (const card of document.querySelectorAll('.settings-extra')) card.classList.toggle('in-tab', card.dataset.tab === name);
    if (remember !== false) store.set('settingsTab', name);
  }

  async function loadSettings() {
    try {
      const data = await api('/api/settings');
      const form = $('settings-form');
      for (const [name, options] of Object.entries(data.choices)) {
        const select = form.elements[name];
        if (!select || select.options.length) continue;
        for (const option of options) {
          const o = document.createElement('option');
          o.value = option;
          o.textContent = (LABELS[name] && LABELS[name][option]) || option;
          select.appendChild(o);
        }
      }
      for (const [key, value] of Object.entries(data.settings)) {
        const field = form.elements[key];
        if (!field) continue;
        if (field.type === 'checkbox') field.checked = !!value; else field.value = value;
      }
    } catch (e) { $('settings-msg').textContent = 'Could not load settings: ' + e.message; }
  }

  async function saveSettings(event) {
    event.preventDefault();
    const form = $('settings-form');
    const body = {};
    for (const field of form.elements) {
      if (!field.name) continue;
      if (field.type === 'checkbox') body[field.name] = field.checked;
      else if (['fps', 'max_viewers', 'compat_max_height', 'android_max_size', 'android_bit_rate', 'android_fps'].includes(field.name)) body[field.name] = parseInt(field.value, 10);
      else body[field.name] = field.value;
    }
    $('settings-msg').textContent = 'Saving…';
    try {
      const result = await api('/api/settings', { method: 'PUT', body });
      const renamed = result.changed.includes('receiver_name') && result.receiver_restarting;
      $('settings-msg').textContent = !result.changed.length ? 'No changes to save.'
        : renamed ? 'Saved. Devices show the new name in a few seconds.'
        : 'Saved' + (result.receiver_restarting ? '. Wi-Fi mirroring is restarting.' : '.');
    } catch (e) { $('settings-msg').textContent = e.message; }
  }

  // ---------------------------------------------------------------- appearance
  function buildAppearance() {
    const theme = window.mirrordeskTheme;
    if (!theme) { $('appearance').hidden = true; return; }
    const groups = [['theme-options', theme.THEMES, 'theme'], ['color-options', theme.COLORS, 'color']];
    for (const [id, items, key] of groups) {
      const container = $(id);
      for (const item of items) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'swatch';
        button.setAttribute('role', 'radio');
        button.dataset.value = item.id;
        const chip = document.createElement('span');
        chip.className = 'chip chip-' + item.id;
        chip.appendChild(icon('check'));
        const label = document.createElement('span');
        label.textContent = item.name;
        button.append(chip, label);
        button.onclick = () => {
          if (key === 'theme' && item.id !== 'light' && item.id !== 'system') store.set('lastDark', item.id);
          theme.set({ [key]: item.id });
        };
        container.appendChild(button);
      }
    }
    theme.onChange(syncAppearance);
    syncAppearance();
  }

  function syncAppearance() {
    const theme = window.mirrordeskTheme;
    for (const [id, value] of [['theme-options', theme.theme], ['color-options', theme.color]]) {
      for (const button of $(id).children) button.setAttribute('aria-checked', String(button.dataset.value === value));
    }
    const light = theme.resolved === 'light';
    $('btn-theme').querySelector('use').setAttribute('href', light ? '#i-moon' : '#i-sun');
    $('btn-theme').title = light ? 'Switch to dark' : 'Switch to light';
  }

  function toggleTheme() {
    const theme = window.mirrordeskTheme;
    if (!theme) return;
    if (theme.resolved !== 'light') { store.set('lastDark', theme.resolved); theme.set({ theme: 'light' }); }
    else theme.set({ theme: store.get('lastDark', 'dark') });
  }

  // ---------------------------------------------------------------- share page
  function kindLabel(kind) {
    return { ethernet: 'Ethernet', wifi: 'Wi-Fi', usb_tether: 'USB network', tailscale: 'Tailscale', vpn: 'VPN or remote access',
      virtual: 'Virtual', loopback: 'This computer only', mdns: 'Local name', other: 'Other network' }[kind] || kind;
  }

  const share = { urls: [], code: '', qrFor: '' };

  // The address a person elsewhere can most likely open: Tailscale works from anywhere on the
  // owner's tailnet; otherwise this computer's own network.
  function bestAddress(urls) {
    const rank = (u) => (u.kind === 'tailscale' ? (u.label === 'Tailscale MagicDNS' ? 0 : 1) : { ethernet: 2, wifi: 2, usb_tether: 4, vpn: 5, other: 6, mdns: 7 }[u.kind] || 8);
    return urls.slice().sort((a, b) => rank(a) - rank(b))[0];
  }

  function inviteLink() {
    const base = $('invite-address').value;
    if (!base) return '';
    return $('invite-with-code').checked && share.code ? base + 'login?code=' + encodeURIComponent(share.code) : base;
  }

  async function updateInvite() {
    const link = inviteLink();
    $('invite-url').value = link;
    const remote = (share.urls.find((u) => u.url === $('invite-address').value) || {}).kind;
    $('invite-hint').textContent = remote === 'tailscale'
      ? 'Tailscale address: works for people on your Tailscale network, wherever they are.'
      : 'This address works on the same network as this computer. People elsewhere need Tailscale.';
    if (!link || link === share.qrFor) return;
    share.qrFor = link;
    try {
      const r = await api('/api/qr', { method: 'POST', body: { text: link } });
      if (share.qrFor === link) $('invite-qr').src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(r.svg);
    } catch (e) { $('invite-qr').removeAttribute('src'); }
  }

  // The viewer code (Viewers page) and the owner code (Settings → Access).
  async function loadAccess() {
    const access = await api('/api/access');
    share.code = access.access_code;
    $('access-code').textContent = access.access_code;
    $('btn-code').hidden = access.pinned;  // set by MIRRORDESK_ACCESS_CODE: rotate it there
    $('owner-code').textContent = access.owner_code;
    $('btn-owner-code').hidden = access.owner_pinned;  // MIRRORDESK_OWNER_CODE
    $('owner-code-hint').textContent = 'Full control from your other devices. Keep it to yourself.';
    $('require-local').checked = !!access.require_code_on_localhost;
    $('require-local').closest('label').hidden = !access.trust_localhost;   // containers always ask
    return access;
  }

  async function loadNetwork(refresh) {
    try {
      const [net] = await Promise.all([api('/api/network' + (refresh ? '?refresh=1' : '')), loadAccess()]);
      // Invitation: every address a viewer could use, the likeliest first.
      const urls = net.urls.filter((u) => u.kind !== 'loopback');
      share.urls = urls;
      const select = $('invite-address');
      const previous = select.value;
      select.textContent = '';
      const best = bestAddress(urls);
      for (const item of urls) {
        const o = document.createElement('option');
        o.value = item.url; o.textContent = kindLabel(item.kind) + ' — ' + item.url;
        select.appendChild(o);
      }
      if (urls.some((u) => u.url === previous)) select.value = previous;
      else if (best) select.value = best.url;
      updateInvite();
      // All addresses and adapters.
      const list = $('urls'); list.textContent = '';
      for (const item of net.urls) {
        const li = document.createElement('li');
        const a = document.createElement('a'); a.href = item.url; a.textContent = item.url; a.target = '_blank'; a.rel = 'noopener';
        const kind = el('span', 'kind', kindLabel(item.kind));
        const copy = el('button', 'icon-btn'); copy.title = 'Copy address';
        copy.setAttribute('aria-label', 'Copy ' + item.url);
        copy.appendChild(icon('copy'));
        copy.onclick = () => copyText(item.url);
        li.append(a, kind, copy); list.appendChild(li);
      }
      const body = $('ifaces'); body.textContent = '';
      for (const iface of net.interfaces) {
        const tr = document.createElement('tr');
        for (const value of [kindLabel(iface.kind), iface.label, iface.ipv4.join(', ')]) tr.appendChild(el('td', '', value));
        body.appendChild(tr);
      }
      const ts = net.tailscale;
      $('tailscale-line').textContent = !ts ? 'Tailscale is not installed on this computer.' : ts.running ? 'Tailscale is connected' + (ts.dns_name ? ' as ' + ts.dns_name : '') + '. Devices on your Tailscale network can use the Tailscale address.' : 'Tailscale is installed but not connected.';
      $('usb-line').textContent = net.usb_tether.length ? 'A USB network is connected. Devices on it can use its address.' : '';
      $('usb-line').hidden = !net.usb_tether.length;
    } catch (e) { toast('Could not load network details: ' + e.message); }
  }

  async function setRequireLocal() {
    const box = $('require-local');
    if (box.checked && !confirm('This computer will then ask for the owner code too: ' + $('owner-code').textContent +
                                '\n\nWrite it down first. Turn it on?')) { box.checked = false; return; }
    box.disabled = true;
    try {
      await api('/api/settings', { method: 'PUT', body: { require_code_on_localhost: box.checked } });
      toast(box.checked ? 'This computer now asks for the owner code too.' : 'This computer opens MirrorDesk without a code again.', 5000);
    } catch (e) { box.checked = !box.checked; toast(e.message, 6000); }
    finally { box.disabled = false; }
  }

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) { navigator.clipboard.writeText(text).then(() => toast('Copied'), () => toast(text, 8000)); return; }
    const area = document.createElement('textarea'); area.value = text; document.body.appendChild(area); area.select();
    try { document.execCommand('copy'); toast('Copied'); } catch (e) { toast(text, 8000); }
    area.remove();
  }

  async function action(path, confirmText) {
    if (confirmText && !confirm(confirmText)) return;
    try { await api(path, { method: 'POST', timeout: 30000 }); } catch (e) { toast(e.message); }
  }

  // ---------------------------------------------------------------- desktop app (Windows, macOS, Linux desktop)
  let appOff = false, quitAt = 0;  // appOff: false | 'quitting' (still shutting down) | 'off'
  let desktopOs = 'windows';
  const desktopText = {
    windows: { signIn: 'sign in to Windows', hint: 'Quit MirrorDesk with Quit at the bottom left of this page, or with its icon next to the clock (it may be under the ^ arrow).',
               restart: 'the MirrorDesk shortcut on the desktop or in the Start menu' },
    macos: { signIn: 'log in to this Mac', hint: 'Quit MirrorDesk with Quit at the bottom left of this page, or with its icon in the Dock.',
             restart: 'MirrorDesk in Applications' },
    linux: { signIn: 'log in', hint: 'Quit MirrorDesk with Quit at the bottom left of this page, or with "Quit MirrorDesk" in the app menu.',
             restart: '"MirrorDesk" in the app menu' },
  };
  async function loadDesktop() {
    try {
      const d = await api('/api/desktop');
      desktopOs = desktopText[d.os] ? d.os : 'windows';
      $('app-card').hidden = false;
      $('autostart').checked = !!d.autostart;
      $('autostart').closest('label').hidden = d.autostart === null;
      $('autostart-label').textContent = 'Start MirrorDesk when I ' + desktopText[desktopOs].signIn;
      $('app-hint').textContent = desktopText[desktopOs].hint;
      // Quit only on the computer itself: from another device MirrorDesk could not be started again.
      const local = !!(status && status.client && status.client.local);
      $('btn-quit').hidden = !local;
      for (const item of document.querySelectorAll('.desktop-only')) item.hidden = !local;
    } catch (e) { $('app-card').hidden = true; $('btn-quit').hidden = true; }  // Docker/Linux server: stop the container or service instead
  }

  // "Quit MirrorDesk?" with "Don't ask me again" (the confirm_quit setting; Settings → General turns it back on).
  async function confirmQuit() {
    if (status && status.settings && status.settings.confirm_quit === false) return true;
    const dialog = $('quit-dialog');
    if (typeof dialog.showModal !== 'function') return confirm('Quit MirrorDesk? Mirroring stops and viewers are disconnected.');
    $('quit-dont-ask').checked = false;
    dialog.returnValue = '';
    dialog.showModal();
    const choice = await new Promise((resolve) => dialog.addEventListener('close', () => resolve(dialog.returnValue), { once: true }));
    if (choice !== 'quit') return false;
    if ($('quit-dont-ask').checked) {
      try { await api('/api/settings', { method: 'PUT', body: { confirm_quit: false } }); } catch (e) { /* asks again next time */ }
    }
    return true;
  }

  async function setAutostart() {
    const box = $('autostart');
    box.disabled = true;
    try {
      const d = await api('/api/desktop/autostart', { method: 'POST', body: { enabled: box.checked } });
      box.checked = !!d.autostart;
      $('app-msg').textContent = d.autostart ? 'MirrorDesk will start when you ' + desktopText[desktopOs].signIn + '.' : 'MirrorDesk will not start by itself.';
    } catch (e) { box.checked = !box.checked; $('app-msg').textContent = e.message; }
    finally { box.disabled = false; }
  }

  async function quitApp() {
    if (!(await confirmQuit())) return;
    try { await api('/api/desktop/quit', { method: 'POST' }); } catch (e) { toast(e.message); return; }
    showQuit();
  }

  // MirrorDesk quit (this page's Quit, its icon, or another of its windows): MirrorDesk's own window
  // closes with it; a tab in another browser on this computer says so and closes itself where the
  // browser allows it. Other devices' pages stay and pick up again when MirrorDesk is back.
  async function showQuit() {
    const local = !!(status && status.client && status.client.local);
    appOff = 'quitting'; quitAt = Date.now();
    selectPage('mirror');
    lastMedia = false;
    await disconnect();
    stage.classList.remove('live');
    $('idle').hidden = false; $('idle-steps').hidden = true; $('idle-sub').hidden = true;
    $('idle-title').textContent = 'MirrorDesk is off';
    $('idle-hint').textContent = local
      ? 'Start it again with ' + desktopText[desktopOs].restart + ' (or ' + desktopText[desktopOs].signIn + ' if it starts automatically). You can close this tab.'
      : 'This page picks up again by itself when MirrorDesk is started again.';
    setChip('chip-receiver', 'Off', 'muted'); setChip('chip-phone', '–', 'muted'); setChip('chip-viewers', '–', 'muted');
    if (local) setTimeout(() => window.close(), 300);
  }

  // ---------------------------------------------------------------- fullscreen / wake lock
  async function toggleFullscreen() {
    const doc = document;
    if (doc.fullscreenElement || doc.webkitFullscreenElement) { (doc.exitFullscreen || doc.webkitExitFullscreen).call(doc); return; }
    const request = stage.requestFullscreen || stage.webkitRequestFullscreen;
    if (request) { try { await request.call(stage); return; } catch (e) { /* fall through */ } }
    if (video.webkitEnterFullscreen) video.webkitEnterFullscreen();  // iPhone Safari
  }

  let wakeLock = null;
  async function keepAwake() {
    if (!('wakeLock' in navigator) || wakeLock || document.hidden) return;
    try { wakeLock = await navigator.wakeLock.request('screen'); wakeLock.addEventListener('release', () => { wakeLock = null; }); } catch (e) { /* not allowed */ }
  }

  // The player bar hides 5 s after the pointer last moved, also with the pointer resting on the
  // picture (this window being screen-shared). Hidden, it takes no clicks. It stays while the
  // mouse is on the bar itself or the keyboard is in it.
  const QUIET_MS = 5000;
  let quietTimer, overControls = false;
  function wake() { stage.classList.remove('quiet'); clearTimeout(quietTimer); quietTimer = setTimeout(quiet, QUIET_MS); }
  function quiet() {
    let keyboard = false;
    try { keyboard = !!$('controls').querySelector(':focus-visible'); } catch (e) { /* older browser */ }
    if (overControls || keyboard) wake(); else stage.classList.add('quiet');
  }

  // ---------------------------------------------------------------- developer page
  function renderDeveloper(s, usb, android) {
    const rx = s.receiver, src = s.source;
    $('rx-source').textContent = describeSource(src);
    $('rx-version').textContent = (rx.version ? 'UxPlay ' + rx.version : 'UxPlay') + ', ' + rx.platform + (s.platform.container ? ', container' : '') + ', requested ' + rx.resolution + ' at ' + rx.fps + ' fps';
    $('dev-input').textContent = { usb: 'USB cable (iPhone)', android: 'Android' }[src.input] || 'Wi-Fi (AirPlay)';
    $('dev-usb').textContent = (usb.sessions || 0) + ' started, ' + (usb.errors || 0) + ' failed';
    const tools = android.tools || {};
    $('dev-android').textContent = (tools.adb ? 'adb ' + (tools.adb_version || '?') : 'adb missing') + ', scrcpy ' + (tools.scrcpy || '?') +
      ', ' + (android.sessions || 0) + ' sessions, ' + (android.errors || 0) + ' failed';
    const rows = $('viewer-rows');
    rows.textContent = '';
    for (const v of s.viewers.list) {
      const tr = document.createElement('tr');
      const mode = { passthrough: 'Original', compat: 'Re-encoded' }[v.mode] || v.mode;
      const state = v.state ? v.state.charAt(0).toUpperCase() + v.state.slice(1) : v.state;
      for (const value of [v.id, mode, v.codec, state, v.local ? 'This computer' : 'Network']) tr.appendChild(el('td', '', value || '–'));
      rows.appendChild(tr);
    }
  }

  async function loadLog() {
    try { const data = await api('/api/receiver/log'); $('rx-log').textContent = data.lines.map((l) => new Date(l.t * 1000).toLocaleTimeString() + '  ' + l.line).join('\n') || '(no output yet)'; }
    catch (e) { $('rx-log').textContent = e.message; }
  }

  async function runCheck() {
    const out = $('check-out'); out.hidden = false; out.textContent = 'Checking (up to 25 s)…'; $('btn-check').disabled = true;
    try { const r = await api('/api/network/check', { method: 'POST', timeout: 30000 }); out.textContent = r.lines.join('\n'); }
    catch (e) { out.textContent = e.message; }
    finally { $('btn-check').disabled = false; }
  }

  async function markIssue() {
    try { await api('/api/diagnostics/mark', { method: 'POST', body: { issue: $('issue').value } }); $('diag-msg').textContent = 'Issue marked.'; }
    catch (e) { $('diag-msg').textContent = 'Could not mark: ' + e.message; }
  }

  async function exportDiagnostics() {
    $('btn-export').disabled = true;
    try {
      const response = await api('/api/diagnostics/export', { method: 'POST', timeout: 30000 });
      const blob = await response.blob();
      const match = /filename="([^"]+)"/.exec(response.headers.get('Content-Disposition') || '');
      const link = document.createElement('a');
      link.href = URL.createObjectURL(blob); link.download = match ? match[1] : 'MirrorDesk-diagnostics.zip';
      document.body.appendChild(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(link.href), 10000);
      $('diag-msg').textContent = 'Diagnostics downloaded. Review before sharing.';
    } catch (e) { $('diag-msg').textContent = e.message; }
    finally { $('btn-export').disabled = false; }
  }

  // ---------------------------------------------------------------- wiring
  function init() {
    $('mode').value = store.get('mode', 'auto');
    $('mode').onchange = () => {
      store.set('mode', $('mode').value); store.set('forceCompat', false); store.set('compatCodec', null);
      viewer.compatTried = false; viewer.sessionCompat = false;
      if (hasMedia(status)) connect();
    };
    $('btn-disconnect').onclick = disconnectPhone;
    $('btn-now-disconnect').onclick = disconnectPhone;
    $('btn-sound').onclick = () => { video.muted = !video.muted; if (!video.muted) video.volume = 1; play(); updateSoundButton(); };
    video.addEventListener('volumechange', updateSoundButton);
    $('tap').onclick = () => { video.muted = false; play(); updateSoundButton(); };
    $('btn-full').onclick = toggleFullscreen;
    video.addEventListener('dblclick', () => { if (!control.on) toggleFullscreen(); });   // a double tap is for the phone
    for (const type of ['pointerdown', 'pointermove', 'pointerup', 'pointercancel']) video.addEventListener(type, onPointer);
    video.addEventListener('wheel', onWheel, { passive: false });
    stage.addEventListener('keydown', onKey);
    stage.addEventListener('paste', onPaste);
    for (const button of document.querySelectorAll('#phone-keys .ctl')) button.onclick = () => sendControl({ t: 'key', k: button.dataset.key });
    $('btn-android-install').onclick = () => androidPost('install-tools');
    $('btn-android-reconnect').onclick = () => androidPost('reconnect');
    $('btn-android-disconnect').onclick = () => {
      const active = status && status.android && (status.android.devices || []).find((d) => d.active);
      if (active) androidPost('disconnect', { serial: active.serial });
    };
    $('android-pick').onchange = () => androidPost('select', { serial: $('android-pick').value });
    $('btn-android-qr').onclick = showPairingQr;
    $('android-pair-form').onsubmit = submitPairing;
    $('btn-android-log').onclick = loadAndroidLog;
    $('android-log-card').addEventListener('toggle', () => { if ($('android-log-card').open) loadAndroidLog(); });
    $('btn-stats').onclick = () => {
      $('stats').hidden = !$('stats').hidden;
      $('btn-stats').classList.toggle('on', !$('stats').hidden);
      store.set('stats', !$('stats').hidden);
    };
    $('stats').hidden = !store.get('stats', false);
    $('btn-stats').classList.toggle('on', !$('stats').hidden);
    if (document.pictureInPictureEnabled) {
      $('btn-pip').hidden = false;
      $('btn-pip').onclick = () => (document.pictureInPictureElement ? document.exitPictureInPicture() : video.requestPictureInPicture()).catch((e) => toast(e.message));
    }
    video.addEventListener('playing', keepAwake);
    document.addEventListener('visibilitychange', () => { if (!document.hidden && !video.paused) keepAwake(); });
    stage.addEventListener('pointermove', wake); stage.addEventListener('touchstart', wake, { passive: true }); wake();
    $('controls').addEventListener('pointerenter', (event) => { overControls = event.pointerType === 'mouse'; });
    $('controls').addEventListener('pointerleave', () => { overControls = false; });
    document.addEventListener('keydown', (event) => { if (event.key === 'Tab') wake(); });   // a hidden bar takes no focus
    for (const item of document.querySelectorAll('.nav-item')) item.onclick = () => selectPage(item.dataset.page);
    $('btn-panel-close').onclick = () => selectPage('mirror');
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && !$('panel').hidden && !document.fullscreenElement) selectPage('mirror');
    });
    // Small screens show either the mirrored screen or a page, so they start on the screen.
    // The remembered page opens once the server confirms this browser is the owner's.
    const narrow = window.matchMedia && window.matchMedia('(max-width: 1099px)').matches;
    startPage = narrow ? 'mirror' : store.get('page', 'mirror');
    selectPage('mirror', false);
    $('nav-owner').onclick = () => { location.href = '/login'; };
    $('tab-iphone').onclick = () => selectPhoneTab('iphone');
    $('tab-android').onclick = () => selectPhoneTab('android');
    selectPhoneTab(store.get('phoneTab', 'iphone') === 'android' ? 'android' : 'iphone', false);
    $('way-android-help').onclick = () => {
      selectPage('connect');
      selectPhoneTab('android');
      $('android-usb-help').open = true;
      $('android-usb-help').scrollIntoView({ block: 'nearest' });
    };
    for (const tab of SETTINGS_TABS) $('stab-' + tab).onclick = () => selectSettingsTab(tab);
    selectSettingsTab(store.get('settingsTab', 'general'), false);
    buildAppearance();
    $('btn-theme').onclick = toggleTheme;
    $('settings-form').onsubmit = saveSettings;
    $('rx-start').onclick = () => action('/api/receiver/start');
    $('rx-stop').onclick = () => action('/api/receiver/stop', 'Stop Wi-Fi mirroring? Phones will no longer see this computer.');
    $('rx-restart').onclick = () => action('/api/receiver/restart');
    $('btn-usb-reconnect').onclick = () => action('/api/usb/reconnect');
    $('btn-usb-driver').onclick = () => usbDriver('driver-install');
    $('btn-usb-restore').onclick = () => usbDriver('driver-restore');
    $('btn-pin').onclick = () => action('/api/regenerate/pin', 'Make a new code? Wi-Fi mirroring restarts, and every iPhone and iPad has to enter the new code.');
    $('btn-code').onclick = async () => {
      if (!confirm('Make a new viewer code? Everyone watching with the old one is signed out and needs the new link or code.')) return;
      try { const r = await api('/api/regenerate/access-code', { method: 'POST' }); share.code = r.access_code; $('access-code').textContent = r.access_code; updateInvite(); }
      catch (e) { toast(e.message); }
    };
    $('btn-owner-code').onclick = async () => {
      if (!confirm('Make a new owner code? Your other devices signed in with the owner code will need to sign in again.')) return;
      try { const r = await api('/api/regenerate/owner-code', { method: 'POST' }); $('owner-code').textContent = r.owner_code; }
      catch (e) { toast(e.message); }
    };
    $('invite-address').onchange = updateInvite;
    $('invite-with-code').onchange = updateInvite;
    $('btn-invite-copy').onclick = () => { if ($('invite-url').value) copyText($('invite-url').value); };
    $('invite-url').onfocus = () => $('invite-url').select();
    $('require-local').onchange = setRequireLocal;
    $('btn-log').onclick = loadLog;
    $('log-card').addEventListener('toggle', () => { if ($('log-card').open) loadLog(); });
    $('btn-net-refresh').onclick = () => loadNetwork(true);
    $('btn-check').onclick = runCheck;
    $('btn-mark').onclick = markIssue;
    $('btn-export').onclick = exportDiagnostics;
    $('autostart').onchange = setAutostart;
    $('btn-quit').onclick = quitApp;
    window.addEventListener('pagehide', () => { if (viewer.id) api('/api/viewers/' + viewer.id, { method: 'DELETE', keepalive: true }).catch(() => {}); });
    if (!window.RTCPeerConnection) viewer.wanted = false;  // status polling must not try to connect either
    updateSoundButton();
    poll().then(loadDesktop);
  }

  window.mirrordesk = { viewer, get status() { return status; }, connect, disconnect };
  init();
})();
