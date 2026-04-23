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
import TasksPage from "./pages/TasksPage";
import StatusPage from "./pages/StatusPage";
import AiAssistantPage from "./pages/AiAssistantPage";
import AiSessionsListPage from "./pages/AiSessionsListPage";
import AiSessionDetailPage from "./pages/AiSessionDetailPage";
import AgentTasksListPage from "./pages/AgentTasksListPage";
import AgentTaskDetailPage from "./pages/AgentTaskDetailPage";
import LoginPage from "./pages/LoginPage";
import { RequireAdmin, RequireAuth, useHomePath } from "./lib/auth";

export default function App() {
  return (
    <Routes>
      {/* Public landing — anonymous users see the login page; logged-in
          users get redirected to their per-role home. */}
      <Route path="/login" element={<LoginPage />} />
      <Route path="/" element={<HomeRedirect />} />

      {/* Legacy redirects — keep old /families links working. */}
      <Route path="/families" element={<Navigate to="/admin/families" replace />} />
      <Route
        path="/families/:familyId/*"
        element={<LegacyFamilyRedirect />}
      />

      {/* Admin console (CRUD + dashboard). The families list is admin-only;
          per-family pages allow members to view their own household.
          Sensitive CRUD pages are gated with <RequireAdmin>. */}
      <Route
        path="/admin/families"
        element={
          <RequireAdmin>
            <FamiliesList />
          </RequireAdmin>
        }
      />
      <Route
        element={
          <RequireAuth>
            <Layout />
          </RequireAuth>
        }
      >
        <Route path="/admin/families/:familyId" element={<FamilyDashboard />} />
        <Route
          path="/admin/families/:familyId/settings"
          element={
            <RequireAdmin>
              <FamilySettings />
            </RequireAdmin>
          }
        />
        <Route
          path="/admin/families/:familyId/people"
          element={
            <RequireAdmin>
              <PeoplePage />
            </RequireAdmin>
          }
        />
        <Route
          path="/admin/families/:familyId/people/:personId"
          element={
            <RequireAdmin>
              <PersonDetail />
            </RequireAdmin>
          }
        />
        <Route
          path="/admin/families/:familyId/relationships"
          element={
            <RequireAdmin>
              <RelationshipsPage />
            </RequireAdmin>
          }
        />
        <Route
          path="/admin/families/:familyId/assistant"
          element={
            <RequireAdmin>
              <AssistantPage />
            </RequireAdmin>
          }
        />
        <Route
          path="/admin/families/:familyId/vehicles"
          element={
            <RequireAdmin>
              <VehiclesPage />
            </RequireAdmin>
          }
        />
        <Route
          path="/admin/families/:familyId/pets"
          element={
            <RequireAdmin>
              <PetsPage />
            </RequireAdmin>
          }
        />
        <Route
          path="/admin/families/:familyId/residences"
          element={
            <RequireAdmin>
              <ResidencesPage />
            </RequireAdmin>
          }
        />
        <Route
          path="/admin/families/:familyId/insurance"
          element={
            <RequireAdmin>
              <InsurancePoliciesPage />
            </RequireAdmin>
          }
        />
        <Route
          path="/admin/families/:familyId/finances"
          element={
            <RequireAdmin>
              <FinancialAccountsPage />
            </RequireAdmin>
          }
        />
        <Route
          path="/admin/families/:familyId/documents"
          element={
            <RequireAdmin>
              <DocumentsPage />
            </RequireAdmin>
          }
        />
        {/* Tasks page is shared — backend filters to created/assigned/follower
            for members, and admins see the full board. */}
        <Route path="/admin/families/:familyId/tasks" element={<TasksPage />} />
        <Route
          path="/admin/families/:familyId/status"
          element={
            <RequireAdmin>
              <StatusPage />
            </RequireAdmin>
          }
        />
      </Route>

      {/* Live AI assistant — members can use it for their own family. The
          backend enforces require_family_member on every endpoint, so a
          member who hand-types another family's id gets a 403 from the API. */}
      <Route
        path="/aiassistant/:familyId"
        element={
          <RequireAuth>
            <AiAssistantPage />
          </RequireAuth>
        }
      />
      <Route
        path="/aiassistant/:familyId/sessions"
        element={
          <RequireAuth>
            <AiSessionsListPage />
          </RequireAuth>
        }
      />
      <Route
        path="/aiassistant/:familyId/sessions/:sessionId"
        element={
          <RequireAuth>
            <AiSessionDetailPage />
          </RequireAuth>
        }
      />
      <Route
        path="/aiassistant/:familyId/agent-tasks"
        element={
          <RequireAdmin>
            <AgentTasksListPage />
          </RequireAdmin>
        }
      />
      <Route
        path="/aiassistant/:familyId/agent-tasks/:taskId"
        element={
          <RequireAdmin>
            <AgentTaskDetailPage />
          </RequireAdmin>
        }
      />

      <Route path="*" element={<HomeRedirect />} />
    </Routes>
  );
}

// Resolve "/" (and unknown paths) at runtime against the cached /me
// response. Avoids hard-coding a destination in the JSX so a member
// landing on `/` lands on their family overview instead of bouncing
// through the admin families list (which they can't see anyway).
function HomeRedirect() {
  const home = useHomePath();
  return <Navigate to={home} replace />;
}

// Preserve deep links shared before the /admin refactor — rewrite the path
// in place instead of sending every old link back to the top-level list.
function LegacyFamilyRedirect() {
  const url = new URL(window.location.href);
  const rewritten = url.pathname.replace(/^\/families/, "/admin/families");
  return <Navigate to={rewritten + url.search + url.hash} replace />;
}
