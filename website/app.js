const EASE = "cubic-bezier(0.32, 0.72, 0, 1)";

function prefersReducedMotion() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function setupNav() {
  const toggle = document.querySelector("[data-nav-toggle]");
  const overlay = document.querySelector("[data-nav-overlay]");
  const burger = document.querySelector("[data-burger]");
  if (!toggle || !overlay || !burger) return;

  const setOpen = (open) => {
    toggle.setAttribute("aria-expanded", String(open));
    overlay.classList.toggle("is-open", open);
    burger.classList.toggle("is-open", open);
    overlay.setAttribute("aria-hidden", String(!open));
    document.body.style.overflow = open ? "hidden" : "";
  };

  toggle.addEventListener("click", () => {
    setOpen(toggle.getAttribute("aria-expanded") !== "true");
  });

  overlay.querySelectorAll("a").forEach((link) => {
    link.addEventListener("click", () => setOpen(false));
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") setOpen(false);
  });
}

function setupReveals() {
  const nodes = document.querySelectorAll(".reveal");
  if (!nodes.length) return;
  if (prefersReducedMotion()) {
    nodes.forEach((node) => node.classList.add("is-visible"));
    return;
  }
  const observer = new IntersectionObserver(
    (entries, obs) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        entry.target.classList.add("is-visible");
        obs.unobserve(entry.target);
      });
    },
    { threshold: 0.18, rootMargin: "0px 0px -8% 0px" }
  );
  nodes.forEach((node) => observer.observe(node));
}

function setupTagline() {
  const root = document.querySelector("[data-tagline]");
  if (!root) return;
  const words = Array.from(root.querySelectorAll(".tagline-word"));
  if (prefersReducedMotion()) {
    words.forEach((word) => word.classList.add("is-lit"));
    return;
  }
  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        const word = entry.target;
        if (entry.intersectionRatio > 0.6) word.classList.add("is-lit");
      });
    },
    { threshold: [0.6], rootMargin: "-42% 0px -42% 0px" }
  );
  words.forEach((word) => observer.observe(word));
}

function setupSectionSpy() {
  const links = Array.from(document.querySelectorAll("[data-section-link]"));
  const sections = links
    .map((link) => document.querySelector(link.getAttribute("href")))
    .filter(Boolean);
  if (!links.length || !sections.length) return;

  const mark = (id) => {
    links.forEach((link) => {
      const active = link.getAttribute("href") === `#${id}`;
      if (active) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
  };

  const observer = new IntersectionObserver(
    (entries) => {
      const visible = entries
        .filter((entry) => entry.isIntersecting)
        .sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
      if (visible?.target?.id) mark(visible.target.id);
    },
    { threshold: [0.35, 0.55], rootMargin: "-20% 0px -45% 0px" }
  );
  sections.forEach((section) => observer.observe(section));
}

function setupCopyButtons() {
  document.querySelectorAll("[data-copy]").forEach((button) => {
    const original = button.textContent;
    button.addEventListener("click", async () => {
      const value = button.getAttribute("data-copy") || "";
      try {
        await navigator.clipboard.writeText(value);
        button.textContent = "Copied";
        window.setTimeout(() => {
          button.textContent = original;
        }, 1600);
      } catch {
        button.textContent = "Copy failed";
        window.setTimeout(() => {
          button.textContent = original;
        }, 1600);
      }
    });
  });
}

function validEmail(value) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value);
}

function setupHelpForm() {
  const form = document.querySelector("[data-help-form]");
  if (!form) return;
  const email = form.querySelector("#help-email");
  const message = form.querySelector("#help-message");
  const machine = form.querySelector("#help-machine");
  const error = form.querySelector("[data-form-error]");
  const success = form.querySelector("[data-form-success]");
  const loading = form.querySelector("[data-form-loading]");
  const submit = form.querySelector("[type='submit']");

  const showError = (text, field) => {
    error.hidden = false;
    error.textContent = text;
    success.hidden = true;
    if (field) {
      field.setAttribute("aria-invalid", "true");
      field.focus();
    }
  };

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    email.removeAttribute("aria-invalid");
    message.removeAttribute("aria-invalid");
    if (!email.value.trim()) {
      showError("Email is required.", email);
      return;
    }
    if (!validEmail(email.value.trim())) {
      showError("Enter an email with a name, an @, and a domain.", email);
      return;
    }
    if (!message.value.trim()) {
      showError("Describe the setup question so maintainers can help.", message);
      return;
    }
    error.hidden = true;
    loading.hidden = false;
    submit.disabled = true;
    const title = encodeURIComponent("Setup question from the website");
    const body = encodeURIComponent(
      [
        `From: ${email.value.trim()}`,
        `Mac: ${machine.value.trim() || "not specified"}`,
        "",
        message.value.trim(),
      ].join("\n")
    );
    window.setTimeout(() => {
      loading.hidden = true;
      success.hidden = false;
      submit.disabled = false;
      success.querySelector("a").href =
        `https://github.com/fbzz/mlx-swarm/issues/new?title=${title}&body=${body}`;
    }, 700);
  });
}

function setupCursorGlow() {
  const glow = document.querySelector("[data-cursor-glow]");
  if (!glow || prefersReducedMotion() || window.matchMedia("(pointer: coarse)").matches) {
    if (glow) glow.hidden = true;
    return;
  }
  window.addEventListener("pointermove", (event) => {
    glow.style.transform = `translate3d(${event.clientX}px, ${event.clientY}px, 0)`;
  });
}

document.documentElement.style.setProperty("--ease-fluid", EASE);
setupNav();
setupReveals();
setupTagline();
setupSectionSpy();
setupCopyButtons();
setupHelpForm();
setupCursorGlow();
