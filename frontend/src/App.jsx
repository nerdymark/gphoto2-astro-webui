import { useState } from "react";
import "./App.css";
import { useCameraStatus, useGalleries, useGallery } from "./hooks/useCamera";
import { useJobs } from "./hooks/useJobs";
import StatusBadge from "./components/StatusBadge";
import ExposureControls from "./components/ExposureControls";
import GalleryManager from "./components/GalleryManager";
import CapturePanel from "./components/CapturePanel";
import TimelapsePanel from "./components/TimelapsePanel";
import GalleryViewer from "./components/GalleryViewer";
import JobsPanel from "./components/JobsPanel";

const TABS = ["Capture", "Gallery", "Timelapse", "Jobs"];

export default function App() {
  const { status } = useCameraStatus();
  const { galleries, refresh: refreshGalleries } = useGalleries();
  const [selectedGallery, setSelectedGallery] = useState(null);
  const { images, refresh: refreshImages } = useGallery(selectedGallery);
  const [tab, setTab] = useState("Capture");
  const { jobsList, activeJobDetail, hasActiveJobs, cancelJob, refresh: refreshJobs } = useJobs();

  const handleCapture = () => {
    refreshImages();
    refreshGalleries();
    refreshJobs();
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
        <div className="flex items-center gap-3">
          {hasActiveJobs && (
            <button
              onClick={() => setTab("Jobs")}
              className="flex items-center gap-1.5 px-2 py-1 rounded-md bg-blue-500/20 border border-blue-500/30 text-blue-400 text-xs font-medium animate-pulse hover:bg-blue-500/30 transition-colors"
            >
              <span className="w-1.5 h-1.5 rounded-full bg-blue-400" />
              Job running
            </button>
          )}
          <StatusBadge
            connected={status?.connected ?? false}
            shootingMode={status?.shooting_mode}
            focusMode={status?.focus_mode}
            battery={status?.battery}
          />
        </div>
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
            {/* Breadcrumb / gallery nav */}
            {selectedGallery && (
              <div className="flex items-center gap-2 text-sm">
                <button
                  onClick={() => setSelectedGallery(null)}
                  className="text-slate-400 hover:text-slate-200 transition-colors"
                >
                  Galleries
                </button>
                <span className="text-slate-600">/</span>
                <span className="text-indigo-400 font-medium">{selectedGallery}</span>
              </div>
            )}

            {/* Tab bar */}
            <div className="flex gap-1 bg-slate-800 rounded-lg p-1 border border-slate-700">
              {TABS.map((t) => (
                <button
                  key={t}
                  onClick={() => setTab(t)}
                  className={`flex-1 rounded-md py-1.5 text-sm font-medium transition-colors relative ${
                    tab === t
                      ? "bg-indigo-600 text-white"
                      : "text-slate-400 hover:text-slate-200"
                  }`}
                >
                  {t}
                  {t === "Jobs" && hasActiveJobs && tab !== "Jobs" && (
                    <span className="absolute top-1 right-1 w-2 h-2 rounded-full bg-blue-400 animate-pulse" />
                  )}
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
            {tab === "Timelapse" && (
              <TimelapsePanel
                gallery={selectedGallery}
                images={images}
                onComplete={handleCapture}
              />
            )}
            {tab === "Jobs" && (
              <JobsPanel
                jobsList={jobsList}
                activeJobDetail={activeJobDetail}
                onCancel={cancelJob}
              />
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
