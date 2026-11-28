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
        const state = getPasswordState();

        if (!state.allPassed) {
            updatePasswordChecklist();
            document.getElementById('password').focus();
            showFieldError(
                document.getElementById('password'),
                'Your password does not meet the requirements below yet.'
            );
            return false;
        }

        if (!state.matches) {
            updatePasswordChecklist();
            document.getElementById('confirm_password').focus();
            showFieldError(
                document.getElementById('confirm_password'),
                'Both passwords must match.'
            );
            return false;
        }

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
                body: JSON.stringify({ device_id: deviceId, role: 'family' })
            });
            const data = await res.json();

            if (!data.valid) {
                showFieldError(deviceInput, data.message || 'Invalid Device ID.');
                if (btn) { btn.disabled = false; btn.textContent = 'Continue'; }
                return; // STOP — do not proceed to step 3
            }

            // Device is taken AND full — no room for another family member.
            if (data.already_registered && !data.can_join) {
                showFieldError(deviceInput, data.message || 'This device has no free family slots left.');
                if (btn) { btn.disabled = false; btn.textContent = 'Continue'; }
                return;
            }

            // Any family member after the first one joins the existing elder.
            if (data.already_registered) {
                applyJoinMode(data);
            } else {
                clearJoinMode();
            }
        } catch (err) {
            console.error(err);
            showFieldError(deviceInput, 'There was a problem verifying the Device ID. Please try again.');
            if (btn) { btn.disabled = false; btn.textContent = 'Continue'; }
            return;
        }

        if (btn) { btn.disabled = false; btn.textContent = 'Continue'; }
    }

    // 3. Join mode: the address is already on file, so step 4 is skipped.
    if (joinContext && currentStep === 3) {
        fillReview();
        showStep(5);
        return;
    }

    // 4. Fill the review fields right before showing the Review step
    if (currentStep === 4) fillReview();

    // 5. Finally, advance to the next step
    showStep(currentStep + 1);
}

function prevStep(current) {
    // Join mode skips step 4 (Home Address), so going back from Review
    // must land on step 3, not step 4.
    if (joinContext && current === 5) {
        showStep(3);
        return;
    }
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
    setReviewText('rev_family_barangay', document.getElementById('family_barangay')?.value);

    // Join mode never collects an address — it is already on the elder profile.
    if (joinContext) {
        setReviewText('rev_address', 'Already on file for this loved one');
    } else {
        const house = document.getElementById('house_no')?.value || '';
        const street = document.getElementById('street')?.value || '';
        const brgy = document.getElementById('barangay')?.value || '';
        const city = document.getElementById('city')?.value || '';
        setReviewText('rev_address', [house, street, brgy, city].filter(Boolean).join(', '));
    }
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

// The browser's built-in QR reader (Chrome/Edge on Android, Safari 17+) is
// faster and reads small or blurry codes better; jsQR is the fallback.
let nativeDetector = null;
try {
    if ('BarcodeDetector' in window) nativeDetector = new BarcodeDetector({ formats: ['qr_code'] });
} catch (e) { nativeDetector = null; }

function startCamera() {
    const video = document.getElementById('qrVideo');
    const hint = document.getElementById('scannerHint');

    // Live camera only works on HTTPS (or localhost). Over plain http on a
    // phone, offer the photo option straight away instead of failing.
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.isSecureContext) {
        hint.textContent = 'The live camera is not available here. Tap "Use a photo instead" to take a picture of the QR code.';
        return;
    }

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
            hint.textContent = err && err.name === 'NotAllowedError'
                ? 'Camera permission was blocked. Allow the camera in your browser settings, or tap "Use a photo instead".'
                : 'Could not start the camera. Tap "Use a photo instead", or type the serial number.';
            console.error('Camera error:', err);
        });
}

// Reads a QR code from an image/canvas/video. Returns the text or null.
async function decodeQr(source, width, height) {
    if (nativeDetector) {
        try {
            const found = await nativeDetector.detect(source);
            if (found.length && found[0].rawValue) return found[0].rawValue.trim();
        } catch (e) { /* fall through to jsQR */ }
    }
    if (typeof jsQR === 'undefined') return null;
    const canvas = document.getElementById('qrCanvas');
    // Big photos are scaled down: jsQR is slow on 12-megapixel images.
    const scale = Math.min(1, 1000 / Math.max(width, height));
    canvas.width = Math.round(width * scale);
    canvas.height = Math.round(height * scale);
    const ctx = canvas.getContext('2d', { willReadFrequently: true });
    ctx.drawImage(source, 0, 0, canvas.width, canvas.height);
    const img = ctx.getImageData(0, 0, canvas.width, canvas.height);
    const code = jsQR(img.data, img.width, img.height, { inversionAttempts: 'attemptBoth' });
    return code && code.data ? code.data.trim() : null;
}

// Fallback when the live camera isn't available: decode a photo.
function scanFromPhoto(input) {
    const file = input.files && input.files[0];
    input.value = '';
    if (!file) return;
    const hint = document.getElementById('scannerHint');
    hint.classList.remove('scan-success');
    hint.textContent = 'Reading the QR code from your photo...';
    const img = new Image();
    img.onload = async () => {
        const text = await decodeQr(img, img.naturalWidth, img.naturalHeight);
        URL.revokeObjectURL(img.src);
        if (text) {
            handleScanSuccess(text);
        } else {
            hint.textContent = 'No QR code found in that photo. Try again closer and with good light, or type the serial number.';
        }
    };
    img.onerror = () => { hint.textContent = 'That file could not be opened as a photo.'; };
    img.src = URL.createObjectURL(file);
}

// Keeps the sticker preview in step 2 showing what was typed or scanned.
function updateStickerSerial() {
    const input = document.getElementById('device_id');
    const out = document.getElementById('stickerSerial');
    if (input && out) out.textContent = input.value.trim() || '—';
}
document.addEventListener('DOMContentLoaded', () => {
    const input = document.getElementById('device_id');
    if (input) {
        input.addEventListener('input', () => { updateStickerSerial(); clearErrors(); });
        // Enter in the serial field = Continue (it's not the final submit).
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                nextStep(2, document.querySelector('.wizard-step[data-step="2"] .btn-submit'));
            }
        });
        updateStickerSerial();
    }
});

function stopCamera() {
    scanning = false;
    if (scannerStream) {
        scannerStream.getTracks().forEach((track) => track.stop());
        scannerStream = null;
    }
}

async function scanLoop() {
    if (!scanning) return;

    const video = document.getElementById('qrVideo');
    if (video.readyState === video.HAVE_ENOUGH_DATA) {
        const text = await decodeQr(video, video.videoWidth, video.videoHeight);
        if (text && scanning) {
            handleScanSuccess(text);
            return; // stop looping, result found
        }
    }
    // ~8 tries a second is plenty and keeps phones from heating up.
    setTimeout(() => requestAnimationFrame(scanLoop), 120);
}

function handleScanSuccess(deviceId) {
    stopCamera();
    if (navigator.vibrate) navigator.vibrate(60);   // feedback on phones

    const hint = document.getElementById('scannerHint');
    hint.textContent = 'QR code detected! Processing...';
    hint.classList.add('scan-success');

    // small pause so the "detected" state is visible before closing
    setTimeout(() => {
        closeScanner();

        const deviceInput = document.getElementById('device_id');
        deviceInput.value = deviceId;
        updateStickerSerial();
        clearErrors(); // clear any "required" error that may be showing

        // Like the app: check the device right away, no extra tap needed.
        const continueBtn = document.querySelector('.wizard-step[data-step="2"] .btn-submit');
        nextStep(2, continueBtn);
    }, 400);
}

// ============================================================
// PASSWORD RULES — live checklist under the password fields
// ============================================================

const PASSWORD_RULES = [
    { key: 'length',  test: p => p.length >= 6 },
    { key: 'upper',   test: p => /[A-Z]/.test(p) },
    { key: 'lower',   test: p => /[a-z]/.test(p) },
    { key: 'number',  test: p => /\d/.test(p) },
    { key: 'special', test: p => /[^A-Za-z0-9]/.test(p) },
];

function getPasswordState() {
    const pwEl = document.getElementById('password');
    const confirmEl = document.getElementById('confirm_password');
    const pw = pwEl ? pwEl.value : '';
    const confirm = confirmEl ? confirmEl.value : '';

    return {
        password: pw,
        confirm: confirm,
        allPassed: PASSWORD_RULES.every(r => r.test(pw)),
        matches: pw.length > 0 && pw === confirm,
    };
}

function updatePasswordChecklist() {
    const list = document.getElementById('passwordRules');
    if (!list) return;

    const state = getPasswordState();
    const typed = state.password.length > 0;

    PASSWORD_RULES.forEach(rule => {
        const li = list.querySelector(`[data-rule="${rule.key}"]`);
        if (!li) return;
        li.classList.remove('ok', 'bad');
        if (!typed) return;                       // stay neutral until they start typing
        li.classList.add(rule.test(state.password) ? 'ok' : 'bad');
    });

    const matchLi = list.querySelector('[data-rule="match"]');
    if (matchLi) {
        matchLi.classList.remove('ok', 'bad');
        if (state.confirm.length > 0) {
            matchLi.classList.add(state.matches ? 'ok' : 'bad');
        }
    }
}

document.addEventListener('DOMContentLoaded', function () {
    ['password', 'confirm_password'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.addEventListener('input', updatePasswordChecklist);
    });
    updatePasswordChecklist();
});

// ============================================================
// JOIN MODE — every family member after the first, on a device
// that is already linked to an elder
// ============================================================

let joinContext = null;

function lockField(el) {
    if (!el) return;
    el.readOnly = true;                 // readOnly, NOT disabled — disabled fields don't submit
    el.classList.add('field-locked');
}

function unlockField(el) {
    if (!el) return;
    el.readOnly = false;
    el.classList.remove('field-locked');
}

function applyJoinMode(data) {
    joinContext = data;

    document.getElementById('join_mode').value = 'join';
    document.getElementById('existing_elder_id').value = data.elder_id || '';

    const nameEl = document.getElementById('senior_full_name');
    const dobEl = document.getElementById('dob');

    if (nameEl) { nameEl.value = data.elder_full_name || ''; lockField(nameEl); }
    if (dobEl)  { dobEl.value  = data.elder_dob || '';       lockField(dobEl); }
    // The join card below shows these, so the locked inputs are hidden.
    [nameEl, dobEl].forEach(el => { const g = el && el.closest('.form-group'); if (g) g.style.display = 'none'; });

    const notice = document.getElementById('joinNotice');
    const noticeText = document.getElementById('joinNoticeText');
    if (notice && noticeText) {
        const who = data.elder_full_name || data.elder_display_name || 'this loved one';
        noticeText.textContent =
            `This device is already registered to ${who}. Check that this is the right ` +
            `person, then just tell us how you are related.`;
        notice.style.display = 'flex';
    }

    // Who the device belongs to, like the app's Join screen.
    const card = document.getElementById('joinCard');
    if (card) {
        const name = (data.elder_full_name || data.elder_display_name || '').trim();
        const parts = name.split(/\s+/).filter(Boolean);
        const initials = parts.length === 0 ? '?'
            : parts.length === 1 ? parts[0][0]
            : parts[0][0] + parts[parts.length - 1][0];
        const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v || 'Not on file'; };
        set('joinAvatar', initials.toUpperCase());
        set('joinName', name || 'Your loved one');
        set('joinSlots', `${data.family_count} of ${data.family_limit} family members linked`);
        set('joinDob', data.elder_dob);
        set('joinSex', data.elder_sex);
        set('joinAddress', data.elder_address);
        const status = { online: 'Online', weak: 'Weak signal', offline: 'Offline' }[data.device_status];
        set('joinDevice', `${document.getElementById('device_id').value.trim()}${status ? ' · ' + status : ''}`);
        card.style.display = 'block';
    }

    // Step 4 is skipped in join mode, so its fields must not block anything.
    document
        .querySelectorAll('.wizard-step[data-step="4"] input, .wizard-step[data-step="4"] select')
        .forEach(el => el.removeAttribute('required'));

    if (typeof lucide !== 'undefined') lucide.createIcons();
}

function clearJoinMode() {
    joinContext = null;

    document.getElementById('join_mode').value = 'claim';
    document.getElementById('existing_elder_id').value = '';

    const nameEl = document.getElementById('senior_full_name');
    const dobEl = document.getElementById('dob');

    if (nameEl) { unlockField(nameEl); nameEl.value = ''; }
    if (dobEl)  { unlockField(dobEl);  dobEl.value = ''; }
    [nameEl, dobEl].forEach(el => { const g = el && el.closest('.form-group'); if (g) g.style.display = ''; });

    const notice = document.getElementById('joinNotice');
    if (notice) notice.style.display = 'none';
    const card = document.getElementById('joinCard');
    if (card) card.style.display = 'none';

    document
        .querySelectorAll('.wizard-step[data-step="4"] input, .wizard-step[data-step="4"] select')
        .forEach(el => el.setAttribute('required', 'required'));
}

// ============================================================
// BARANGAY + HEALTH CENTER DROPDOWNS — options come from Firestore
// (GET /barangays), nothing is hardcoded in the HTML.
// ============================================================
let healthCentersByBarangay = {};

function fillSelect(select, placeholder, values) {
    select.innerHTML = '';
    const first = document.createElement('option');
    first.value = '';
    first.textContent = placeholder;
    select.appendChild(first);
    values.forEach((v) => {
        const opt = document.createElement('option');
        opt.value = v;
        opt.textContent = v;
        select.appendChild(opt);
    });
}

// BHW: when a barangay is picked, show that barangay's health center(s).
function updateHealthCenters() {
    const brgy = document.getElementById('barangay_assigned');
    const hc = document.getElementById('health_center');
    if (!brgy || !hc) return;

    const centers = healthCentersByBarangay[brgy.value] || [];
    fillSelect(hc, brgy.value ? 'Select your health center' : 'Select your barangay first', centers);
    if (centers.length === 1) hc.value = centers[0]; // only one -> pre-select it
}

async function loadBarangays() {
    const familySel = document.getElementById('family_barangay');
    const bhwSel = document.getElementById('barangay_assigned');
    if (!familySel && !bhwSel) return;

    try {
        const res = await fetch('/barangays');
        const data = await res.json();
        if (!data.success || !data.barangays.length) throw new Error('empty');

        healthCentersByBarangay = data.health_centers || {};
        if (familySel) fillSelect(familySel, 'Select your barangay', data.barangays);
        if (bhwSel) {
            fillSelect(bhwSel, 'Select your assigned barangay', data.barangays);
            bhwSel.addEventListener('change', updateHealthCenters);
            updateHealthCenters();
        }
    } catch (err) {
        console.error('Could not load barangays:', err);
        const msg = 'Could not load barangays — refresh the page';
        if (familySel) fillSelect(familySel, msg, []);
        if (bhwSel) fillSelect(bhwSel, msg, []);
    }
}

document.addEventListener('DOMContentLoaded', loadBarangays);