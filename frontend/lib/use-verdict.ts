"use client";
import { useEffect, useState } from "react";
import type { Snapshot } from "./types";

export function useVerdict(dataset: string, pinned: string | null, refresh: number) {
  const [data, setData] = useState<Snapshot | null>(null);
  const [status, setStatus] = useState("Connecting");
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let active = true, socket: WebSocket | null = null, retry: ReturnType<typeof setTimeout>;
    let cursor = 0, delay = 1000, chain = Promise.resolve();
    const controller = new AbortController();
    setData(previous => previous?.dataset === dataset ? previous : null); setError(null); setStatus(pinned ? "Historical round" : "Connecting");
    async function load() {
      const params = new URLSearchParams({ dataset });
      if (pinned) params.set("assessment_id", pinned);
      const response = await fetch(`/api/verdict/dashboard?${params}`, { cache: "no-store", signal: controller.signal });
      if (!response.ok) throw new Error(response.status === 404 ? "This round is not available in the selected dataset." : "The evidence service is unavailable. Retrying connection…");
      const next: Snapshot = await response.json();
      if (next.dataset !== dataset) throw new Error("Dataset mismatch; reload the selected dataset.");
      if (active) { setData(next); setError(null); }
      return next;
    }
    function connect() {
      if (!active || pinned) return;
      const root = process.env.NEXT_PUBLIC_VERDICT_WS_URL || `${location.protocol === "https:" ? "wss" : "ws"}://${location.hostname}:8000/v1/stream`;
      const url = new URL(root); url.searchParams.set("dataset", dataset); url.searchParams.set("after", String(cursor));
      socket = new WebSocket(url);
      socket.onopen = () => { if (active) { delay = 1000; setStatus("Connected"); } };
      socket.onmessage = event => {
        chain = chain.then(async () => {
          if (!active) return;
          const message = JSON.parse(event.data);
          if (message.type !== "batch" || message.dataset !== dataset) return;
          if (message.events.length) await load();
          // Advance only after the snapshot succeeds. Duplicate round notifications
          // are harmless; failed loads are replayed after reconnect.
          cursor = Math.max(cursor, message.cursor);
        }).catch(() => { if (active) { setStatus("Reconnecting"); socket?.close(); } });
      };
      socket.onerror = () => socket?.close();
      socket.onclose = event => {
        if (!active) return;
        setStatus("Reconnecting");
        retry = setTimeout(async () => {
          try {
            if (event.code === 1008) cursor = (await load()).cursor;
            connect();
          } catch { if (active) retry = setTimeout(connect, delay); }
        }, delay);
        delay = Math.min(delay * 2, 15000);
      };
    }
    async function boot() {
      try { cursor = (await load()).cursor; connect(); }
      catch (e) {
        if (!active) return;
        setError(e instanceof Error ? e.message : "Unable to load evidence."); setStatus("Disconnected");
        retry = setTimeout(boot, 3000);
      }
    }
    void boot();
    return () => { active = false; controller.abort(); clearTimeout(retry); socket?.close(); };
  }, [dataset, pinned, refresh]);
  return { data, status, error };
}
