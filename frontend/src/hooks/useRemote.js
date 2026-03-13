import { useState, useEffect } from "react";
import * as api from "../api/client";

export function useRemoteStatus() {
  const [remoteStatus, setRemoteStatus] = useState(null);

  useEffect(() => {
    let mounted = true;
    const check = async () => {
      try {
        const data = await api.getRemoteStatus();
        if (mounted) setRemoteStatus(data);
      } catch {
        if (mounted) setRemoteStatus(null);
      }
    };
    check();
    const interval = setInterval(check, 30000); // refresh every 30s
    return () => { mounted = false; clearInterval(interval); };
  }, []);

  return remoteStatus;
}
