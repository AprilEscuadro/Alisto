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
    document.getElementById("terms").checked = true;
    closeModal("termsModal");
}

function acceptPrivacy() {
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
            showFieldError(input, "This field is required.");
            return false;
        }
    }

    if (step === 1) {
        const terms = document.getElementById('terms');
        if (!terms.checked) {
            showTermsError("Please read and agree to the Terms and Privacy Policy first.");
            return false;
        }
    }

    if (step === 3) {
        const relSelect = document.getElementById('relationship_select');
        if (relSelect && !relSelect.value) {
            showFieldError(relSelect, "This field is required.");
            return false;
        }
        if (relSelect && relSelect.value === 'other') {
            const otherInput = document.getElementById('relationship_other_input');
            if (!otherInput.value.trim()) {
                showFieldError(otherInput, "Please specify the relationship.");
                return false;
            }
        }
        updateRelationshipValue();
    }

    return true;
}

function showFieldError(input, message) {
    clearErrors();
    const wrap = input.closest('.form-group') || input.parentElement;
    const err = document.createElement('div');
    err.className = 'field-error';
    err.textContent = message;
    wrap.appendChild(err);
}

function showTermsError(message) {
    clearErrors();
    const termsRow = document.querySelector('.terms-row');
    const err = document.createElement('div');
    err.className = 'field-error terms-error';
    err.textContent = message;
    termsRow.insertAdjacentElement('afterend', err);
}

function clearErrors() {
    document.querySelectorAll('.field-error').forEach(el => el.remove());
}

// ============================================================
// RELATIONSHIP "OTHER" HANDLING — auto-shows the specify field
// ============================================================
function handleRelationshipChange() {
    const relSelect = document.getElementById('relationship_select');
    const otherField = document.getElementById('relationshipOtherField');
    const otherInput = document.getElementById('relationship_other_input');

    clearErrors();

    if (relSelect.value === 'other') {
        otherField.style.display = 'block';
        otherInput.setAttribute('required', 'required');
        otherInput.focus();
    } else {
        otherField.style.display = 'none';
        otherInput.removeAttribute('required');
        otherInput.value = '';
    }

    updateRelationshipValue();
}

function updateRelationshipValue() {
    const relSelect = document.getElementById('relationship_select');
    const otherInput = document.getElementById('relationship_other_input');
    const hiddenField = document.getElementById('relationship');

    if (!relSelect || !hiddenField) return;

    if (relSelect.value === 'other') {
        const specify = otherInput.value.trim();
        hiddenField.value = specify ? `Other (${specify})` : 'Other';
    } else {
        const selectedOption = relSelect.options[relSelect.selectedIndex];
        hiddenField.value = selectedOption && selectedOption.value ? selectedOption.text : '';
    }
}

// ============================================================
// NEXT STEP — validates fields, verifies device_id on step 2,
// fills review on step 4, then advances the wizard
// ============================================================
async function nextStep(currentStep, btn) {
    // 1. Validate required fields / terms checkbox for the CURRENT step
    if (!validateStep(currentStep)) return;

    // 2. Special case: step 2 (Device ID) needs async backend verification
    if (currentStep === 2) {
        const deviceInput = document.getElementById('device_id');
        const deviceId = deviceInput.value.trim();

        if (btn) {
            btn.disabled = true;
            btn.textContent = 'Checking...';
        }

        try {
            const res = await fetch('/check_device', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ device_id: deviceId })
            });
            const data = await res.json();

            if (!data.valid) {
                showFieldError(deviceInput, data.message || 'Invalid Device ID.');
                if (btn) {
                    btn.disabled = false;
                    btn.textContent = 'Continue';
                }
                return; // STOP — do not proceed to step 3
            }
        } catch (err) {
            console.error(err);
            showFieldError(deviceInput, 'There was a problem verifying the Device ID. Please try again.');
            if (btn) {
                btn.disabled = false;
                btn.textContent = 'Continue';
            }
            return;
        }

        if (btn) {
            btn.disabled = false;
            btn.textContent = 'Continue';
        }
    }

    // 3. Fill the review fields right before showing the Review step
    if (currentStep === 4) fillReview();

    // 4. Finally, advance to the next step
    showStep(currentStep + 1);
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

    setReviewText('rev_relationship', document.getElementById('relationship')?.value);

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

// FORM SUBMIT — CONNECT TO FLASK BACKEND
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

document.addEventListener('DOMContentLoaded', function () {
    const step1 = document.querySelector('.wizard-step[data-step="1"]');
    if (step1) {
        step1.querySelectorAll('input, select').forEach(el => {
            el.addEventListener('input', clearErrors);
            el.addEventListener('change', clearErrors);
        });
    }
});

// ============================================================
// QR SCANNER — camera access, live decode, auto-fill Device ID
// ============================================================

let scannerStream = null;
let scanning = false;

function openScanner() {
    document.getElementById('qrScannerModal').classList.add('active');
    document.body.style.overflow = 'hidden';
    resetScannerHint();
    startCamera();
}

function closeScanner() {
    document.getElementById('qrScannerModal').classList.remove('active');
    document.body.style.overflow = 'auto';
    stopCamera();
}

function resetScannerHint() {
    const hint = document.getElementById('scannerHint');
    hint.textContent = 'Point your camera at the QR code on the device.';
    hint.classList.remove('scan-success');
}

function startCamera() {
    const video = document.getElementById('qrVideo');

    navigator.mediaDevices
        .getUserMedia({ video: { facingMode: 'environment' } })
        .then((stream) => {
            scannerStream = stream;
            video.srcObject = stream;
            video.play();
            scanning = true;
            requestAnimationFrame(scanLoop);
        })
        .catch((err) => {
            document.getElementById('scannerHint').textContent =
                'Unable to access the camera. Please allow camera permission in your browser, or use HTTPS/localhost.';
            console.error('Camera error:', err);
        });
}

function stopCamera() {
    scanning = false;
    if (scannerStream) {
        scannerStream.getTracks().forEach((track) => track.stop());
        scannerStream = null;
    }
}

function scanLoop() {
    if (!scanning) return;

    const video = document.getElementById('qrVideo');
    const canvas = document.getElementById('qrCanvas');

    if (video.readyState === video.HAVE_ENOUGH_DATA) {
        canvas.width = video.videoWidth;
        canvas.height = video.videoHeight;
        const ctx = canvas.getContext('2d');
        ctx.drawImage(video, 0, 0, canvas.width, canvas.height);

        const imageData = ctx.getImageData(0, 0, canvas.width, canvas.height);
        const code = jsQR(imageData.data, imageData.width, imageData.height, {
            inversionAttempts: 'dontInvert',
        });

        if (code && code.data) {
            handleScanSuccess(code.data.trim());
            return; // stop looping, result found
        }
    }

    requestAnimationFrame(scanLoop);
}

function handleScanSuccess(deviceId) {
    stopCamera();

    const hint = document.getElementById('scannerHint');
    hint.textContent = 'QR code detected! Processing...';
    hint.classList.add('scan-success');

    // small pause so the "detected" state is visible before closing
    setTimeout(() => {
        closeScanner();

        const deviceInput = document.getElementById('device_id');
        deviceInput.value = deviceId;
        deviceInput.classList.add('input-loading');
        clearErrors(); // clear any "required" error that may be showing

        // simulate brief processing on the field, then auto-continue
        setTimeout(() => {
            deviceInput.classList.remove('input-loading');
            const continueBtn = document.querySelector('.wizard-step[data-step="2"] .btn-submit');
            nextStep(2, continueBtn);
        }, 900);
    }, 500);
}