// WebSocket wrapper: parses preview-frame binary messages and JSON text
// messages, reconnects with capped exponential backoff.

const HEADER_BYTES = 24;
const BACKOFF_BASE_MS = 500;
const BACKOFF_MAX_MS = 10000;

export class ReconnectingSocket {
  constructor(url, handlers) {
    this.url = url;
    this.handlers = handlers;
    this.ws = null;
    this.attempt = 0;
    this.timer = null;
    this.userClosed = false;
  }

  connect() {
    clearTimeout(this.timer);
    this.userClosed = false;
    const ws = new WebSocket(this.url);
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    ws.onopen = () => {
      this.attempt = 0;
      this.handlers.onOpen?.();
    };
    ws.onmessage = (e) => this._onMessage(e);
    ws.onerror = () => ws.close();
    ws.onclose = () => {
      if (this.ws !== ws) return;
      this.ws = null;
      this.handlers.onClose?.();
      if (!this.userClosed) this._scheduleReconnect();
    };
  }

  // User-initiated disconnect: drop the socket and stop auto-reconnecting.
  // Call connect() again to reconnect.
  disconnect() {
    this.userClosed = true;
    clearTimeout(this.timer);
    if (this.ws) this.ws.close();
  }

  send(obj) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(obj));
      return true;
    }
    return false;
  }

  _scheduleReconnect() {
    const delay =
      Math.min(BACKOFF_MAX_MS, BACKOFF_BASE_MS * 2 ** this.attempt) +
      Math.random() * 250;
    this.attempt += 1;
    this.timer = setTimeout(() => this.connect(), delay);
  }

  _onMessage(e) {
    if (e.data instanceof ArrayBuffer) {
      const frame = parseFrame(e.data);
      if (frame) this.handlers.onFrame?.(frame);
      return;
    }
    let msg;
    try {
      msg = JSON.parse(e.data);
    } catch {
      return;
    }
    this.handlers.onJson?.(msg);
  }
}

// 24-byte little-endian header followed by JPEG bytes.
function parseFrame(buf) {
  if (buf.byteLength < HEADER_BYTES) return null;
  const dv = new DataView(buf);
  const version = dv.getUint8(0);
  const kind = dv.getUint8(1);
  if (version !== 1 || kind !== 1) return null;
  return {
    cameraIndex: dv.getUint8(2),
    recording: (dv.getUint8(3) & 1) !== 0,
    frameNumber: dv.getUint32(4, true),
    hwTimestampNs: dv.getBigUint64(8, true),
    fps: dv.getFloat32(16, true),
    dropped: dv.getUint32(20, true),
    jpeg: new Uint8Array(buf, HEADER_BYTES),
  };
}
