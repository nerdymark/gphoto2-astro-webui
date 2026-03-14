/**
 * API client for the gphoto2 Astro WebUI backend.
 */

const BASE = import.meta.env.VITE_API_BASE ?? "";

async function request(path, options = {}) {
  const { timeout = 30000, ...fetchOpts } = options;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const res = await fetch(`${BASE}${path}`, {
      ...fetchOpts,
      signal: controller.signal,
    });
    if (!res.ok) {
      const text = await res.text().catch(() => res.statusText);
      throw new Error(text || res.statusText);
    }
    return res.json();
  } catch (err) {
    if (err.name === "AbortError") {
      throw new Error("Request timed out");
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

// Camera
export const getCameraStatus = () => request("/api/camera/status", { timeout: 8000 });
export const getExposure = () => request("/api/camera/exposure");
export const setExposure = (body) =>
  request("/api/camera/exposure", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
export const captureImage = (gallery) =>
  request("/api/camera/capture", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ gallery }),
  });
export const captureBurst = (gallery, count, interval = 0, bulbSeconds = null, { stack, timelapse, remote: useRemote } = {}) =>
  request("/api/camera/burst", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      gallery,
      count,
      interval,
      ...(bulbSeconds != null && { bulb_seconds: bulbSeconds }),
      ...(stack && { stack }),
      ...(timelapse && { timelapse }),
      ...(useRemote && { remote: true }),
    }),
  });

// Galleries
export const listGalleries = () => request("/api/galleries");
export const createGallery = (name) =>
  request("/api/galleries", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
export const getGallery = (name) => request(`/api/galleries/${encodeURIComponent(name)}`);
export const deleteImage = (gallery, filename) =>
  request(`/api/galleries/${encodeURIComponent(gallery)}/${encodeURIComponent(filename)}`, {
    method: "DELETE",
  });

// Stacking (returns job_id – actual work happens in background)
export const stackImages = (gallery, images, mode, outputName, useRemote = false) =>
  request(`/api/galleries/${encodeURIComponent(gallery)}/stack`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ images, mode, output_name: outputName, remote: useRemote }),
    timeout: 60000,
  });

// Timelapse (returns job_id – ffmpeg runs in background)
export const createTimelapse = (gallery, images, fps, resolution, outputName, useRemote = false) =>
  request(`/api/galleries/${encodeURIComponent(gallery)}/timelapse`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ images, fps, resolution, output_name: outputName, remote: useRemote }),
  });

// Remote processing
export const getRemoteStatus = () => request("/api/remote/status", { timeout: 8000 });

export const videoUrl = (gallery, filename) =>
  `${BASE}/api/videos/${encodeURIComponent(gallery)}/${encodeURIComponent(filename)}`;

// Jobs
export const getJob = (jobId) => request(`/api/jobs/${jobId}`);
export const listJobs = () => request("/api/jobs");
export const cancelJob = (jobId) =>
  request(`/api/jobs/${jobId}/cancel`, { method: "POST" });

export const imageUrl = (gallery, filename) =>
  `${BASE}/api/images/${encodeURIComponent(gallery)}/${encodeURIComponent(filename)}`;

export const thumbnailUrl = (gallery, filename) =>
  `${BASE}/api/thumbnails/${encodeURIComponent(gallery)}/${encodeURIComponent(filename)}`;
