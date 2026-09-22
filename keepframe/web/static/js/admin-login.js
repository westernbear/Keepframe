import { postAdminLogin } from "/static/js/api.js?v=20260921v";
import { T } from "/static/js/i18n.js?v=20260921v";

const TENANTS_PATH = "/admin/tenants";

function readCredentials(form) {
  return {
    email: form.elements.email.value,
    password: form.elements.password.value,
  };
}

document.getElementById("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await postAdminLogin(readCredentials(e.target));
    location.assign(TENANTS_PATH);
  } catch (err) {
    alert(err.message || T("admin.loginFailed"));
  }
});
