import { useState } from "react";
import * as api from "../api/client";
import { imageUrl } from "../api/client";

export default function GalleryViewer({ gallery, images, onRefresh }) {
  const [lightbox, setLightbox] = useState(null);
  const [deleting, setDeleting] = useState(null);

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
          {images.map((img) => (
            <div key={img.filename} className="group relative">
              <button
                onClick={() => setLightbox(img)}
                className="block w-full aspect-square overflow-hidden rounded-lg border border-slate-700 hover:border-indigo-500 transition-colors"
              >
                <img
                  src={imageUrl(gallery, img.filename)}
                  alt={img.filename}
                  className="w-full h-full object-cover"
                  loading="lazy"
                />
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
          onClick={() => setLightbox(null)}
        >
          <div
            className="relative max-w-5xl w-full"
            onClick={(e) => e.stopPropagation()}
          >
            <button
              onClick={() => setLightbox(null)}
              className="absolute -top-10 right-0 text-white text-2xl hover:text-slate-300"
            >
              ✕
            </button>
            <img
              src={imageUrl(gallery, lightbox.filename)}
              alt={lightbox.filename}
              className="w-full rounded-lg max-h-[80vh] object-contain"
            />
            <p className="text-slate-400 text-xs text-center mt-2">{lightbox.filename}</p>
            <a
              href={imageUrl(gallery, lightbox.filename)}
              download={lightbox.filename}
              className="block text-center mt-2 text-indigo-400 text-sm hover:underline"
              onClick={(e) => e.stopPropagation()}
            >
              Download
            </a>
          </div>
        </div>
      )}
    </div>
  );
}
