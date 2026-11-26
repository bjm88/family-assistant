import { useCallback, useEffect, useRef, useState } from "react";
import type { GameAction, GameView } from "./types";

export type SocketStatus = "connecting" | "open" | "closed";

interface Handlers {
  /** The whole game, pushed after every change. */
  onState: (game: GameView) => void;
  /** The server refused something this player just did. */
  onError: (message: string) => void;
  /** The connection was lost (or refused). A retry is already scheduled. */
  onDropped: () => void;
  /** A message that belongs to a level being played live (Zombie Apocalypse). */
  onLive?: (message: LiveMessage) => void;
}

/** Anything other than the game's state and errors: `type` says what. */
export type LiveMessage = { type: string } & Record<string, unknown>;

const PING_EVERY_MS = 25_000;
// After the phone wakes up, how long to wait for any sign of life
// before assuming the old connection is dead.
const WAKE_CHECK_MS = 3_000;
const MAX_RETRY_MS = 5_000;

/**
 * The live connection for one player in one game.
 *
 * Reconnects by itself, with a short backoff, when the connection
 * drops, and checks the connection as soon as the page becomes visible
 * again, because a phone that was locked or switched to another app
 * usually comes back with a dead socket that still looks open.
 */
export function useGameSocket(
  token: string | undefined,
  enabled: boolean,
  handlers: Handlers
): { status: SocketStatus; send: (action: GameAction | LiveMessage) => boolean } {
  const [status, setStatus] = useState<SocketStatus>("connecting");
  const socketRef = useRef<WebSocket | null>(null);
  const handlersRef = useRef(handlers);
  handlersRef.current = handlers;

  useEffect(() => {
    if (!token || !enabled) return;
    let stopped = false;
    let attempt = 0;
    let retryTimer: number | undefined;
    let pingTimer: number | undefined;
    let wakeTimer: number | undefined;

    const url =
      `${window.location.protocol === "https:" ? "wss" : "ws"}://${window.location.host}` +
      `/api/games/play/${encodeURIComponent(token)}/ws`;

    const lost = (socket: WebSocket) => {
      if (socketRef.current !== socket) return;
      socketRef.current = null;
      window.clearInterval(pingTimer);
      window.clearTimeout(wakeTimer);
      socket.onopen = socket.onmessage = socket.onclose = socket.onerror = null;
      try {
        socket.close();
      } catch {
        /* already closed */
      }
      if (stopped) return;
      setStatus("closed");
      handlersRef.current.onDropped();
      const delay = Math.min(MAX_RETRY_MS, 400 * 2 ** attempt);
      attempt += 1;
      retryTimer = window.setTimeout(connect, delay);
    };

    const connect = () => {
      window.clearTimeout(retryTimer);
      if (stopped || socketRef.current) return;
      setStatus("connecting");
      const socket = new WebSocket(url);
      socketRef.current = socket;
      socket.onopen = () => {
        attempt = 0;
        setStatus("open");
        pingTimer = window.setInterval(() => {
          if (socket.readyState === WebSocket.OPEN) socket.send('{"type":"ping"}');
        }, PING_EVERY_MS);
      };
      socket.onmessage = (event) => {
        window.clearTimeout(wakeTimer);
        let message: { type?: string; game?: GameView; message?: string };
        try {
          message = JSON.parse(event.data);
        } catch {
          return;
        }
        if (message.type === "state" && message.game) {
          handlersRef.current.onState(message.game);
        } else if (message.type === "error") {
          handlersRef.current.onError(message.message ?? "That didn't work.");
        } else if (typeof message.type === "string" && message.type !== "pong") {
          handlersRef.current.onLive?.(message as LiveMessage);
        }
      };
      socket.onclose = () => lost(socket);
    };

    const wake = () => {
      if (document.visibilityState !== "visible" || stopped) return;
      const socket = socketRef.current;
      if (!socket) {
        attempt = 0;
        connect();
      } else if (socket.readyState === WebSocket.OPEN) {
        socket.send('{"type":"ping"}');
        window.clearTimeout(wakeTimer);
        wakeTimer = window.setTimeout(() => lost(socket), WAKE_CHECK_MS);
      }
    };

    connect();
    document.addEventListener("visibilitychange", wake);
    window.addEventListener("online", wake);
    return () => {
      stopped = true;
      document.removeEventListener("visibilitychange", wake);
      window.removeEventListener("online", wake);
      window.clearTimeout(retryTimer);
      const socket = socketRef.current;
      if (socket) lost(socket);
    };
  }, [token, enabled]);

  const send = useCallback((action: GameAction | LiveMessage) => {
    const socket = socketRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) return false;
    socket.send(JSON.stringify(action));
    return true;
  }, []);

  return { status, send };
}
