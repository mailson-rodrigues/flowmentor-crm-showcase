// Socket.IO client: messages, status, typing, SLA alerts and presence in real time.
import { auth } from "./api.js";

let sock = null;
const handlers = new Map();

export function connectSocket() {
  if (sock || typeof io === "undefined") return sock;
  sock = io({ path: "/socket.io", auth: (cb) => cb({ token: auth.get()?.access }), transports: ["websocket", "polling"], reconnectionDelayMax: 8000 });
  sock.onAny((event, ...args) => handlers.get(event)?.forEach((fn) => { try { fn(...args); } catch (e) { console.error(e); } }));
  sock.on("connect_error", (err) => { if (String(err?.message).includes("token")) sock?.disconnect(); });
  return sock;
}
export function disconnectSocket() { sock?.disconnect(); sock = null; }
export function on(event, fn) {
  if (!handlers.has(event)) handlers.set(event, new Set());
  handlers.get(event).add(fn);
  return () => handlers.get(event)?.delete(fn);
}
export const emit = (event, data, ack) => (ack ? sock?.emit(event, data, ack) : sock?.emit(event, data));
export const isConnected = () => !!sock?.connected;
