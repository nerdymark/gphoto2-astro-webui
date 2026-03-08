import { useState } from "react";
import "./App.css";
import { useCameraStatus, useGalleries, useGallery } from "./hooks/useCamera";
import StatusBadge from "./components/StatusBadge";
import ExposureControls from "./components/ExposureControls";
import GalleryManager from "./components/GalleryManager";
import CapturePanel from "./components/CapturePanel";
import StackingPanel from "./components/StackingPanel";
import GalleryViewer from "./components/GalleryViewer";

const TABS = ["Capture", "Gallery", "Stacking"];

export default function App() {
  const { status } = useCameraStatus();
  const { galleries, refresh: refreshGalleries } = useGalleries();
  const [selectedGallery, setSelectedGallery] = useState(null);
  const { images, refresh: refreshImages } = useGallery(selectedGallery);
  const [tab, setTab] = useState("Capture");

  const handleCapture = () => {
    refreshImages();
    refreshGalleries();
  };

  return (
    <div className="min-h-screen bg-slate-900 text-slate-100">
      {/* Header */}
      <header className="bg-slate-800 border-b border-slate-700 px-4 py-3 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <span className="text-xl">🔭</span>
          <h1 className="text-base font-bold tracking-tight text-white">
            gphoto2 Astro WebUI
          </h1>
        </div>
        <StatusBadge connected={status?.connected ?? false} />
      </header>

      <div className="max-w-7xl mx-auto px-4 py-6 space-y-6">
        {/* Exposure Controls – always visible */}
        <ExposureControls />

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* Left column: Gallery manager */}
          <div>
            <GalleryManager
              galleries={galleries}
              selectedGallery={selectedGallery}
              onGallerySelect={setSelectedGallery}
              onRefresh={refreshGalleries}
            />
          </div>

          {/* Right column: tabbed panel */}
          <div className="lg:col-span-2 space-y-4">
            {/* Tab bar */}
            <div className="flex gap-1 bg-slate-800 rounded-lg p-1 border border-slate-700">
              {TABS.map((t) => (
                <button
                  key={t}
                  onClick={() => setTab(t)}
                  className={`flex-1 rounded-md py-1.5 text-sm font-medium transition-colors ${
                    tab === t
                      ? "bg-indigo-600 text-white"
                      : "text-slate-400 hover:text-slate-200"
                  }`}
                >
                  {t}
                </button>
              ))}
            </div>

            {tab === "Capture" && (
              <CapturePanel
                gallery={selectedGallery}
                onCapture={handleCapture}
              />
            )}
            {tab === "Gallery" && (
              <GalleryViewer
                gallery={selectedGallery}
                images={images}
                onRefresh={refreshImages}
              />
            )}
            {tab === "Stacking" && (
              <StackingPanel
                gallery={selectedGallery}
                images={images}
                onStackComplete={handleCapture}
              />
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
