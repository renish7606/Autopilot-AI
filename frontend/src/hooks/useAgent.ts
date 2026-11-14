import { useCallback, useEffect, useRef, useState } from "react";
import type { AgentEvent, RunOptions } from "../lib/types";

const MAX_RECONNECTS = 5;
const RECONNECT_DELAY_MS = 3000;

export interface AgentClient {
  events: AgentEvent[];
  isConnected: boolean;
  isRunning: boolean;
  currentSessionId: string | null;
  pendingApproval: AgentEvent | null;
  sendGoal: (goal: string, options?: RunOptions) => void;
  sendApproval: (goal: string, sessionId: string) => void;
  dismissApproval: () => void;
  selectSession: (sessionId: string) => void;
  newSession: () => void;
  appendEvent: (event: AgentEvent) => void;
}

function socketUrl(): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/ws/agent`;
}

export function useAgent(): AgentClient {
  const socketRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);
  const reconnectCountRef = useRef(0);
  const manuallyClosedRef = useRef(false);
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [isConnected, setIsConnected] = useState(false);
  const [isRunning, setIsRunning] = useState(false);
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
  const [pendingApproval, setPendingApproval] = useState<AgentEvent | null>(null);

  const appendEvent = useCallback((event: AgentEvent) => {
    setEvents((current) => [...current, event]);
  }, []);

  useEffect(() => {
    manuallyClosedRef.current = false;
    let disposed = false;

    const connect = () => {
      if (disposed || manuallyClosedRef.current) return;
      const socket = new WebSocket(socketUrl());
      socketRef.current = socket;

      socket.onopen = () => {
        if (disposed) return socket.close();
        reconnectCountRef.current = 0;
        setIsConnected(true);
      };

      socket.onmessage = (message: MessageEvent<string>) => {
        let event: AgentEvent;
        try {
          const parsed: unknown = JSON.parse(message.data);
          if (!parsed || typeof parsed !== "object" || !("event" in parsed)) return;
          event = parsed as AgentEvent;
        } catch {
          appendEvent({ event: "error", message: "Received an invalid event from the server." });
          return;
        }

        if (event.event === "session_created" && typeof event.session_id === "string") {
          setCurrentSessionId(event.session_id);
        }
        if (event.event === "approval_required") setPendingApproval(event);
        if (event.event === "task_started") setIsRunning(true);
        if (["task_completed", "task_failed", "error"].includes(event.event)) setIsRunning(false);
        if (event.event === "task_completed" || event.event === "task_failed") setPendingApproval(null);
        appendEvent(event);
      };

      socket.onerror = () => socket.close();
      socket.onclose = () => {
        if (socketRef.current === socket) socketRef.current = null;
        setIsConnected(false);
        setIsRunning(false);
        if (disposed || manuallyClosedRef.current || reconnectCountRef.current >= MAX_RECONNECTS) return;
        reconnectCountRef.current += 1;
        reconnectTimerRef.current = window.setTimeout(connect, RECONNECT_DELAY_MS);
      };
    };

    connect();
    return () => {
      disposed = true;
      manuallyClosedRef.current = true;
      if (reconnectTimerRef.current !== null) window.clearTimeout(reconnectTimerRef.current);
      socketRef.current?.close();
      socketRef.current = null;
    };
  }, [appendEvent]);

  const send = useCallback((payload: Record<string, unknown>) => {
    const socket = socketRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      appendEvent({ event: "error", message: "Not connected to the agent server. Wait for reconnection and try again." });
      return;
    }
    socket.send(JSON.stringify(payload));
  }, [appendEvent]);

  const sendGoal = useCallback((goal: string, options: RunOptions = {}) => {
    const cleanGoal = goal.trim();
    if (!cleanGoal) return;
    setEvents([]);
    setPendingApproval(null);
    setIsRunning(true);
    send({ type: "run_goal", goal: cleanGoal, ...(currentSessionId ? { session_id: currentSessionId } : {}), ...options });
  }, [currentSessionId, send]);

  const sendApproval = useCallback((goal: string, sessionId: string) => {
    setPendingApproval(null);
    setIsRunning(true);
    send({ type: "approve", goal, session_id: sessionId });
  }, [send]);

  const dismissApproval = useCallback(() => setPendingApproval(null), []);
  const selectSession = useCallback((sessionId: string) => {
    setCurrentSessionId(sessionId);
    setEvents([]);
    setPendingApproval(null);
  }, []);
  const newSession = useCallback(() => {
    setCurrentSessionId(null);
    setEvents([]);
    setPendingApproval(null);
    setIsRunning(false);
  }, []);

  return { events, isConnected, isRunning, currentSessionId, pendingApproval, sendGoal, sendApproval, dismissApproval, selectSession, newSession, appendEvent };
}
