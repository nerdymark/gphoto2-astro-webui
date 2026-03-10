import { useState, useEffect, useCallback } from "react";
import * as api from "../api/client";
import { imageUrl, videoUrl, thumbnailUrl } from "../api/client";

const VIDEO_EXTENSIONS = [".mp4", ".webm"];

function isVideo(filename) {
  return VIDEO_EXTENSIONS.some((ext) => filename.toLowerCase().endsWith(ext));
}

function mediaUrl(gallery, filename) {
  return isVideo(filename) ? videoUrl(gallery, filename) : imageUrl(gallery, filename);
}

export default function GalleryViewer({ gallery, images, onRefresh }) {
  const [lightboxIndex, setLightboxIndex] = useState(null);
  const [deleting, setDeleting] = useState(null);

  const isOpen = lightboxIndex !== null;
  const lightbox = isOpen ? images[lightboxIndex] : null;

  const goPrev = useCallback(() => {
    setLightboxIndex((i) => (i > 0 ? i - 1 : images.length - 1));
  }, [images.length]);

  const goNext = useCallback(() => {
    setLightboxIndex((i) => (i < images.length - 1 ? i + 1 : 0));
  }, [images.length]);

  const close = useCallback(() => setLightboxIndex(null), []);

  useEffect(() => {
    if (!isOpen) return;
    const onKey = (e) => {
      if (e.key === "ArrowLeft") goPrev();
      else if (e.key === "ArrowRight") goNext();
      else if (e.key === "Escape") close();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [isOpen, goPrev, goNext, close]);

  const handleDelete = async (filename) => {
    if (!confirm(`Delete ${filename}?`)) return;
    setDeleting(filename);
    try {
      await api.deleteImage(gallery, filename);
      onRefresh();
    } catch (err) {
      alert(err.message);
    } finally {
      setDeleting(null);
    }
  };

  if (!gallery) {
    return (
      <div className="rounded-xl bg-slate-800 border border-slate-700 p-4">
        <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-300 mb-2">
          Gallery
        </h2>
        <p className="text-slate-500 text-sm">Select a gallery to view images.</p>
      </div>
    );
  }

  return (
    <div className="rounded-xl bg-slate-800 border border-slate-700 p-4 space-y-3">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-300">
          Gallery: <span className="text-indigo-400">{gallery}</span>
        </h2>
        <button
          onClick={onRefresh}
          className="text-xs text-slate-400 hover:text-slate-200 transition-colors"
        >
          ↻ Refresh
        </button>
      </div>

      {images.length === 0 ? (
        <p className="text-slate-500 text-sm">No images yet. Start capturing!</p>
      ) : (
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-2">
          {images.map((img, idx) => (
            <div key={img.filename} className="group relative">
              <button
                onClick={() => setLightboxIndex(idx)}
                className="block w-full aspect-square overflow-hidden rounded-lg border border-slate-700 hover:border-indigo-500 transition-colors relative"
              >
                {isVideo(img.filename) ? (
                  <>
                    <div className="w-full h-full bg-slate-900 flex items-center justify-center">
                      <svg className="w-12 h-12 text-indigo-400" fill="currentColor" viewBox="0 0 20 20">
                        <path
                          fillRule="evenodd"
                          d="M10 18a8 8 0 100-16 8 8 0 000 16zM9.555 7.168A1 1 0 008 8v4a1 1 0 001.555.832l3-2a1 1 0 000-1.664l-3-2z"
                          clipRule="evenodd"
                        />
                      </svg>
                    </div>
                    <span className="absolute bottom-1 left-1 bg-black/70 text-indigo-300 text-[10px] font-semibold px-1.5 py-0.5 rounded">
                      VIDEO
                    </span>
                  </>
                ) : (
                  <img
                    src={thumbnailUrl(gallery, img.filename)}
                    alt={img.filename}
                    className="w-full h-full object-cover"
                    loading="lazy"
                  />
                )}
              </button>
              <p className="text-xs text-slate-500 truncate mt-0.5 px-0.5">
                {img.filename}
              </p>
              <button
                onClick={() => handleDelete(img.filename)}
                disabled={deleting === img.filename}
                className="absolute top-1 right-1 hidden group-hover:flex items-center justify-center w-6 h-6 rounded-full bg-red-700 hover:bg-red-600 text-white text-xs transition-colors"
                title="Delete image"
              >
                ✕
              </button>
            </div>
          ))}
        </div>
      )}

      {/* Lightbox */}
      {lightbox && (
        <div
          className="fixed inset-0 z-50 bg-black/90 flex items-center justify-center p-4"
          onClick={close}
        >
          {/* Previous button */}
          {images.length > 1 && (
            <button
              onClick={(e) => { e.stopPropagation(); goPrev(); }}
              className="absolute left-4 top-1/2 -translate-y-1/2 z-10 w-10 h-10 flex items-center justify-center rounded-full bg-slate-800/70 hover:bg-slate-700 text-white text-xl transition-colors"
              title="Previous (←)"
            >
              ‹
            </button>
          )}

          <div
            className="relative max-w-5xl w-full"
            onClick={(e) => e.stopPropagation()}
          >
            <button
              onClick={close}
              className="absolute -top-10 right-0 text-white text-2xl hover:text-slate-300"
            >
              ✕
            </button>
            {isVideo(lightbox.filename) ? (
              <video
                src={videoUrl(gallery, lightbox.filename)}
                controls
                autoPlay
                className="w-full rounded-lg max-h-[80vh]"
              />
            ) : (
              <img
                src={imageUrl(gallery, lightbox.filename)}
                alt={lightbox.filename}
                className="w-full rounded-lg max-h-[80vh] object-contain"
              />
            )}
            <div className="flex items-center justify-between mt-2 px-1">
              <p className="text-slate-400 text-xs">
                {lightboxIndex + 1} / {images.length}
              </p>
              <p className="text-slate-400 text-xs">{lightbox.filename}</p>
              <a
                href={mediaUrl(gallery, lightbox.filename)}
                download={lightbox.filename}
                className="text-indigo-400 text-sm hover:underline"
                onClick={(e) => e.stopPropagation()}
              >
                Download
              </a>
            </div>
          </div>

          {/* Next button */}
          {images.length > 1 && (
            <button
              onClick={(e) => { e.stopPropagation(); goNext(); }}
              className="absolute right-4 top-1/2 -translate-y-1/2 z-10 w-10 h-10 flex items-center justify-center rounded-full bg-slate-800/70 hover:bg-slate-700 text-white text-xl transition-colors"
              title="Next (→)"
            >
              ›
            </button>
          )}
        </div>
      )}
    </div>
  );
}
