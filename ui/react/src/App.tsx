import { Navigate, Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import FamiliesList from "./pages/FamiliesList";
import FamilyDashboard from "./pages/FamilyDashboard";
import FamilySettings from "./pages/FamilySettings";
import PeoplePage from "./pages/PeoplePage";
import PersonDetail from "./pages/PersonDetail";
import RelationshipsPage from "./pages/RelationshipsPage";
import AssistantPage from "./pages/AssistantPage";
import VehiclesPage from "./pages/VehiclesPage";
import PetsPage from "./pages/PetsPage";
import ResidencesPage from "./pages/ResidencesPage";
import InsurancePoliciesPage from "./pages/InsurancePoliciesPage";
import FinancialAccountsPage from "./pages/FinancialAccountsPage";
import DocumentsPage from "./pages/DocumentsPage";
import AiAssistantPage from "./pages/AiAssistantPage";
import AiSessionsListPage from "./pages/AiSessionsListPage";
import AiSessionDetailPage from "./pages/AiSessionDetailPage";
import AgentTasksListPage from "./pages/AgentTasksListPage";
import AgentTaskDetailPage from "./pages/AgentTaskDetailPage";

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Navigate to="/admin/families" replace />} />

      {/* Legacy redirects — keep old /families links working. */}
      <Route path="/families" element={<Navigate to="/admin/families" replace />} />
      <Route
        path="/families/:familyId/*"
        element={<LegacyFamilyRedirect />}
      />

      {/* Admin console (CRUD + dashboard) */}
      <Route path="/admin/families" element={<FamiliesList />} />
      <Route element={<Layout />}>
        <Route path="/admin/families/:familyId" element={<FamilyDashboard />} />
        <Route path="/admin/families/:familyId/settings" element={<FamilySettings />} />
        <Route path="/admin/families/:familyId/people" element={<PeoplePage />} />
        <Route path="/admin/families/:familyId/people/:personId" element={<PersonDetail />} />
        <Route path="/admin/families/:familyId/relationships" element={<RelationshipsPage />} />
        <Route path="/admin/families/:familyId/assistant" element={<AssistantPage />} />
        <Route path="/admin/families/:familyId/vehicles" element={<VehiclesPage />} />
        <Route path="/admin/families/:familyId/pets" element={<PetsPage />} />
        <Route path="/admin/families/:familyId/residences" element={<ResidencesPage />} />
        <Route path="/admin/families/:familyId/insurance" element={<InsurancePoliciesPage />} />
        <Route path="/admin/families/:familyId/finances" element={<FinancialAccountsPage />} />
        <Route path="/admin/families/:familyId/documents" element={<DocumentsPage />} />
      </Route>

      {/* Live AI assistant (separate top-level namespace so it can evolve
          independently of the admin routes, with different auth later). */}
      <Route path="/aiassistant/:familyId" element={<AiAssistantPage />} />
      <Route
        path="/aiassistant/:familyId/sessions"
        element={<AiSessionsListPage />}
      />
      <Route
        path="/aiassistant/:familyId/sessions/:sessionId"
        element={<AiSessionDetailPage />}
      />
      <Route
        path="/aiassistant/:familyId/agent-tasks"
        element={<AgentTasksListPage />}
      />
      <Route
        path="/aiassistant/:familyId/agent-tasks/:taskId"
        element={<AgentTaskDetailPage />}
      />

      <Route path="*" element={<Navigate to="/admin/families" replace />} />
    </Routes>
  );
}

// Preserve deep links shared before the /admin refactor — rewrite the path
// in place instead of sending every old link back to the top-level list.
function LegacyFamilyRedirect() {
  const url = new URL(window.location.href);
  const rewritten = url.pathname.replace(/^\/families/, "/admin/families");
  return <Navigate to={rewritten + url.search + url.hash} replace />;
}
