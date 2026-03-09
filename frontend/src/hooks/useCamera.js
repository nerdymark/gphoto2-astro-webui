import { useState, useEffect, useCallback, useRef } from "react";
import * as api from "../api/client";

export function useCameraStatus() {
  const [status, setStatus] = useState(null);
  const [loading, setLoading] = useState(true);
  const pendingRef = useRef(false);

  const refresh = useCallback(async () => {
    // Skip if a previous poll is still in-flight to avoid piling up
    // requests behind a slow camera lock.
    if (pendingRef.current) return;
    pendingRef.current = true;
    try {
      const data = await api.getCameraStatus();
      setStatus(data);
    } catch {
      setStatus({ connected: false });
    } finally {
      pendingRef.current = false;
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 5000);
    return () => clearInterval(id);
  }, [refresh]);

  return { status, loading, refresh };
}

export function useExposure() {
  const [exposure, setExposure] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  const refresh = useCallback(async () => {
    try {
      const data = await api.getExposure();
      setExposure(data);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const save = useCallback(
    async (settings) => {
      setSaving(true);
      setError(null);
      try {
        await api.setExposure(settings);
        await refresh();
      } catch (e) {
        setError(e.message);
      } finally {
        setSaving(false);
      }
    },
    [refresh]
  );

  return { exposure, loading, saving, error, save, refresh };
}

export function useGalleries() {
  const [galleries, setGalleries] = useState([]);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const data = await api.listGalleries();
      setGalleries(data.galleries ?? []);
    } catch {
      /* ignore */
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  return { galleries, loading, refresh };
}

export function useGallery(name) {
  const [images, setImages] = useState([]);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    if (!name) return;
    setLoading(true);
    try {
      const data = await api.getGallery(name);
      setImages(data.images ?? []);
    } catch {
      setImages([]);
    } finally {
      setLoading(false);
    }
  }, [name]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  return { images, loading, refresh };
}
