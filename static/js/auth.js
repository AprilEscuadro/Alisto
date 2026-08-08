function toggleBarangayField() {
    const role = document.getElementById('role').value;
    const barangayField = document.getElementById('barangayField');
    barangayField.style.display = role === 'bhw' ? 'block' : 'none';
}

      // Open modals
      function openTermsModal() {
        document.getElementById("termsModal").classList.add("active");
        document.body.style.overflow = "hidden";
      }

      function openPrivacyModal() {
        document.getElementById("privacyModal").classList.add("active");
        document.body.style.overflow = "hidden";
      }

      // Close modal
      function closeModal(modalId) {
        document.getElementById(modalId).classList.remove("active");
        document.body.style.overflow = "auto";
      }

      // Accept terms/privacy
      function acceptTerms() {
        // Check the checkbox
        document.getElementById("terms").checked = true;
        closeModal("termsModal");
      }

      function acceptPrivacy() {
        // Check the checkbox
        document.getElementById("terms").checked = true;
        closeModal("privacyModal");
      }

      // Close modal when clicking outside
      document.querySelectorAll(".modal-overlay").forEach((modal) => {
        modal.addEventListener("click", (e) => {
          if (e.target === modal) {
            closeModal(modal.id);
          }
        });
      });

      // Close modal on Escape key
      document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") {
          document
            .querySelectorAll(".modal-overlay.active")
            .forEach((modal) => {
              closeModal(modal.id);
            });
        }
      });


      lucide.createIcons();

function togglePassword(inputId, iconId) {
    const input = document.getElementById(inputId);
    const icon = document.getElementById(iconId);
    if (input.type === "password") {
        input.type = "text";
        icon.setAttribute("data-lucide", "eye-off");
    } else {
        input.type = "password";
        icon.setAttribute("data-lucide", "eye");
    }
    lucide.createIcons();
}

const totalDots = 5;

function updateDots(step) {
    document.querySelectorAll('.step-dot').forEach(dot => {
        const dotStep = parseInt(dot.dataset.step);
        dot.classList.remove('active', 'done');
        if (dotStep < step) dot.classList.add('done');
        if (dotStep === step) dot.classList.add('active');
    });
}

function showStep(step) {
    document.querySelectorAll('.wizard-step').forEach(el => el.classList.remove('active'));
    document.querySelector(`.wizard-step[data-step="${step}"]`).classList.add('active');

    const progress = document.getElementById('stepProgress');
    if (progress) {
        if (step > totalDots) {
            progress.style.display = 'none';
        } else {
            progress.style.display = 'flex';
            updateDots(step);
        }
    }
}

function validateStep(step) {
    const stepEl = document.querySelector(`.wizard-step[data-step="${step}"]`);
    const inputs = stepEl.querySelectorAll('input[required], select[required]');
    for (const input of inputs) {
        if (!input.value.trim()) {
            input.focus();
            return false;
        }
    }
    return true;
}

function nextStep(current) {
    if (!validateStep(current)) return;
    if (current === 4) fillReview();
    showStep(current + 1);
}

function prevStep(current) {
    showStep(current - 1);
}

function fillReview() {
    setReviewText('rev_account_name', document.getElementById('full_name')?.value);
    setReviewText('rev_email', document.getElementById('email')?.value);

    const roleSel = document.getElementById('role');
    setReviewText('rev_role', roleSel?.options[roleSel.selectedIndex]?.text);

    setReviewText('rev_device_id', document.getElementById('device_id')?.value);

    setReviewText('rev_full_name', document.getElementById('senior_full_name')?.value);
    setReviewText('rev_dob', document.getElementById('dob')?.value);

    const rel = document.getElementById('relationship');
    setReviewText('rev_relationship', rel?.options[rel.selectedIndex]?.text);

    setReviewText('rev_occupation', document.getElementById('occupation')?.value);

    const house = document.getElementById('house_no')?.value || '';
    const street = document.getElementById('street')?.value || '';
    const brgy = document.getElementById('barangay')?.value || '';
    const city = document.getElementById('city')?.value || '';
    setReviewText('rev_address', [house, street, brgy, city].filter(Boolean).join(', '));
}

function setReviewText(elementId, value) {
    const el = document.getElementById(elementId);
    if (el) el.textContent = value || '—';
}

// FORM SUBMIT — I-CONNECT SA FLASK BACKEND
document.addEventListener('DOMContentLoaded', function () {
    const registerForm = document.getElementById('registerForm');
    if (registerForm) {
        registerForm.addEventListener('submit', function (e) {
            e.preventDefault();

            const formData = new FormData(this);
            const submitBtn = registerForm.querySelector('button[type="submit"]');
            if (submitBtn) {
                submitBtn.disabled = true;
                submitBtn.textContent = "Submitting...";
            }

            fetch(registerForm.action, {
                method: "POST",
                body: formData
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    showStep(6);
                } else {
                    alert(data.message || "Registration failed. Please check your inputs.");
                    if (submitBtn) {
                        submitBtn.disabled = false;
                        submitBtn.textContent = "Submit & Complete";
                    }
                }
            })
            .catch(err => {
                console.error(err);
                alert("Something went wrong. Please try again.");
                if (submitBtn) {
                    submitBtn.disabled = false;
                    submitBtn.textContent = "Submit & Complete";
                }
            });
        });
    }

    if (typeof lucide !== 'undefined') {
        lucide.createIcons();
    }
});