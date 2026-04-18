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
import InsurancePoliciesPage from "./pages/InsurancePoliciesPage";
import FinancialAccountsPage from "./pages/FinancialAccountsPage";
import DocumentsPage from "./pages/DocumentsPage";

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Navigate to="/families" replace />} />
      <Route path="/families" element={<FamiliesList />} />
      <Route element={<Layout />}>
        <Route path="/families/:familyId" element={<FamilyDashboard />} />
        <Route path="/families/:familyId/settings" element={<FamilySettings />} />
        <Route path="/families/:familyId/people" element={<PeoplePage />} />
        <Route path="/families/:familyId/people/:personId" element={<PersonDetail />} />
        <Route path="/families/:familyId/relationships" element={<RelationshipsPage />} />
        <Route path="/families/:familyId/assistant" element={<AssistantPage />} />
        <Route path="/families/:familyId/vehicles" element={<VehiclesPage />} />
        <Route path="/families/:familyId/pets" element={<PetsPage />} />
        <Route path="/families/:familyId/insurance" element={<InsurancePoliciesPage />} />
        <Route path="/families/:familyId/finances" element={<FinancialAccountsPage />} />
        <Route path="/families/:familyId/documents" element={<DocumentsPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/families" replace />} />
    </Routes>
  );
}
