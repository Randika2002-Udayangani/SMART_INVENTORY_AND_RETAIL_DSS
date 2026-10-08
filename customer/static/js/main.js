const API_BASE = "http://127.0.0.1:8000/api";

function saveToken(token) {
  sessionStorage.setItem("customer_jwt", token);
}

function getToken() {
  return sessionStorage.getItem("customer_jwt");
}

function clearToken() {
  sessionStorage.removeItem("customer_jwt");
}
function requireAuth() {
  if (!getToken()) {
    window.location.href = "/customer/login/";
  }
}

function logout() {
  clearToken();
  window.location.href = "/customer/login/";
}