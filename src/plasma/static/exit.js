// Changes made with a button are saved as they happen. Only an open form holds anything
// unsaved: the exit button saves it before leaving, and leaving any other way asks first.
const exitForm = document.querySelector(".exit-form");
const openForm = document.querySelector("form.stacked");

if (openForm) {
  const serialize = () => new URLSearchParams(new FormData(openForm));
  const initial = serialize().toString();
  let handled = false;

  openForm.addEventListener("submit", () => {
    handled = true;
  });

  window.addEventListener("beforeunload", (event) => {
    if (!handled && serialize().toString() !== initial) event.preventDefault();
  });

  exitForm.addEventListener("submit", async (event) => {
    handled = true;
    const body = serialize();
    if (body.toString() === initial) return;

    event.preventDefault();
    const response = await fetch(openForm.action, { method: "POST", body });
    if (response.ok) {
      exitForm.submit();
    } else {
      openForm.requestSubmit();
    }
  });
}
