import { Routes, Route, Navigate } from "react-router-dom";
import Portfolio from "./views/Portfolio";
import Engagement from "./views/Engagement";
import { features } from "./features";
import LegacyDashboard from "./legacy/LegacyDashboard";

// ---------------------------------------------------------------------------
// App — route table for the two-level Engagement Ops UI.
//   /          Portfolio (30,000-foot strip)
//   /e/:slug   Engagement (approval queue + commitments + dossiers + projects)
//   /legacy    parked RDMIS dashboard — ONLY when features.legacyPanels is on
// ---------------------------------------------------------------------------

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Portfolio />} />
      <Route path="/e/:slug" element={<Engagement />} />
      {features.legacyPanels && (
        <Route path="/legacy" element={<LegacyDashboard />} />
      )}
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
