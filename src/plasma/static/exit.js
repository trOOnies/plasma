// The exit button saves anything changed in the open form before leaving.
const exitForm = document.querySelector(".exit-form");
const openForm = document.querySelector("form.stacked");

if (openForm) {
  const serialize = () => new URLSearchParams(new FormData(openForm));
  const initial = serialize().toString();

  exitForm.addEventListener("submit", async (event) => {
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
