/*
 * BHW registration flow for register.html
 * ----------------------------------------
 * Family registration still uses nextStep()/prevStep() from auth.js.
 * This file only takes over when the role is "bhw":
 *   Step 1 (account)  ->  bhw-info (professional info)  ->  bhw-review  ->  bhw-pending
 *
 * Fields already filled in step 1 (full name, email, phone, password, barangay)
 * are NOT asked again. They are reused in the review step and sent with the form.
 */
(function () {
  const BHW_DOT_COUNT = 3; // account, professional info, review
  const DOT_FOR_STEP = { '1': 1, 'bhw-info': 2, 'bhw-review': 3 };
  const MAX_ID_SIZE = 2 * 1024 * 1024; // 2MB
  const ALLOWED_ID_EXT = ['jpg', 'jpeg', 'png', 'pdf'];

  const $ = (id) => document.getElementById(id);
  const val = (id) => ($(id) ? $(id).value.trim() : '');
  const isBhw = () => val('role') === 'bhw';

  // ---------- Step switching ----------

  function showStep(name) {
    document.querySelectorAll('#registerForm .wizard-step').forEach((step) => {
      step.classList.toggle('active', step.dataset.step === String(name));
    });
    updateDots(name);
    clearErrors();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  function updateDots(name) {
    const progress = $('stepProgress');
    if (!progress) return;

    if (name === 'bhw-pending') {
      progress.style.display = 'none';
      return;
    }
    progress.style.display = '';

    const current = DOT_FOR_STEP[name];
    progress.querySelectorAll('.step-dot').forEach((dot) => {
      dot.classList.toggle('active', Number(dot.dataset.step) === current);
    });
  }

  // BHW has 3 steps, so hide dots 4 and 5 while "BHW" is selected.
  function syncDotCount() {
    const bhw = isBhw();
    document.querySelectorAll('#stepProgress .step-dot').forEach((dot) => {
      dot.style.display = bhw && Number(dot.dataset.step) > BHW_DOT_COUNT ? 'none' : '';
    });
  }

  // ---------- Errors (reuses the .alert.alert-error style from auth.css) ----------

  function showError(stepName, message) {
    clearErrors();
    const step = document.querySelector(`.wizard-step[data-step="${stepName}"]`);
    if (!step) return;

    const box = document.createElement('div');
    box.className = 'alert alert-error bhw-error';
    box.textContent = message;

    const subtitle = step.querySelector('.auth-subtitle');
    if (subtitle) subtitle.after(box);
    else step.prepend(box);

    box.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  function clearErrors() {
    document.querySelectorAll('.bhw-error').forEach((el) => el.remove());
  }

  // ---------- Validation ----------

  function validateAccount() {
    if (!val('full_name')) return 'Please enter your full name.';
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(val('email'))) return 'Please enter a valid email address.';
    if (!val('barangay_assigned')) return 'Please enter your assigned barangay.';

    const password = $('password').value;
    if (password.length < 6) return 'Password must be at least 6 characters.';
    if (password !== $('confirm_password').value) return 'Password and confirm password do not match.';
    if (!$('terms').checked) return 'Please agree to the Terms and Privacy Policy.';
    return null;
  }

  function validateProfessional() {
    const dob = val('bhw_dob');
    if (!dob) return 'Please enter your date of birth.';
    if (new Date(dob) > new Date()) return 'Date of birth cannot be in the future.';
    if (!val('health_center')) return 'Please select your health center.';
    if (!val('years_of_service')) return 'Please select your years of service.';
    if (!val('bhw_id_number')) return 'Please enter your BHW ID number.';
    return validateFile(currentFile());
  }

  // ---------- Valid ID upload ----------

  function currentFile() {
    const input = $('valid_id');
    return input && input.files.length ? input.files[0] : null;
  }

  function validateFile(file) {
    if (!file) return null; // optional
    const ext = file.name.split('.').pop().toLowerCase();
    if (!ALLOWED_ID_EXT.includes(ext)) return 'Valid ID must be a JPG, PNG, or PDF file.';
    if (file.size > MAX_ID_SIZE) return 'Valid ID must be 2MB or smaller.';
    return null;
  }

  function formatSize(bytes) {
    return bytes < 1024 * 1024
      ? `${Math.max(1, Math.round(bytes / 1024))} KB`
      : `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  function renderFile(file) {
    const zone = $('validIdDropzone');
    $('validIdTitle').textContent = file ? file.name : 'Click to upload or drag and drop';
    $('validIdHint').textContent = file ? formatSize(file.size) : 'JPG, PNG, PDF (Max 2MB)';
    zone.classList.toggle('has-file', !!file);
    $('validIdRemove').style.display = file ? '' : 'none';
  }

  function clearFile() {
    $('valid_id').value = '';
    renderFile(null);
  }

  function handleFile(file) {
    const error = validateFile(file);
    if (error) {
      clearFile();
      showError('bhw-info', error);
      return;
    }
    clearErrors();
    renderFile(file);
  }

  function setupDropzone() {
    const zone = $('validIdDropzone');
    const input = $('valid_id');
    if (!zone || !input) return;

    input.addEventListener('change', () => handleFile(currentFile()));

    ['dragenter', 'dragover'].forEach((evt) =>
      zone.addEventListener(evt, (e) => {
        e.preventDefault();
        zone.classList.add('is-dragover');
      })
    );
    ['dragleave', 'drop'].forEach((evt) =>
      zone.addEventListener(evt, (e) => {
        e.preventDefault();
        zone.classList.remove('is-dragover');
      })
    );
    zone.addEventListener('drop', (e) => {
      const file = e.dataTransfer.files[0];
      if (!file) return;
      const transfer = new DataTransfer();
      transfer.items.add(file);
      input.files = transfer.files;
      handleFile(file);
    });

    $('validIdRemove').addEventListener('click', clearFile);
  }

  // ---------- Review ----------

  function fillReview() {
    const set = (id, text) => {
      $(id).textContent = text || '—';
    };
    const file = currentFile();

    set('bhw_rev_full_name', val('full_name'));
    set('bhw_rev_phone', val('contact_number'));
    set('bhw_rev_email', val('email'));
    set('bhw_rev_barangay', val('barangay_assigned'));
    set('bhw_rev_health_center', val('health_center'));
    set('bhw_rev_bhw_id', val('bhw_id_number'));
    set('bhw_rev_years', val('years_of_service'));
    set('bhw_rev_valid_id', file ? file.name : 'None uploaded');
  }

  // ---------- Public functions (called from register.html) ----------

  // Step 1 "Continue": family keeps the old flow, BHW goes to the professional step.
  window.bhwContinueFromStep1 = function () {
    if (!isBhw()) {
      nextStep(1);
      return;
    }
    const error = validateAccount();
    if (error) {
      showError('1', error);
      return;
    }
    showStep('bhw-info');
  };

  window.bhwShowStep = showStep;

  window.bhwGoToReview = function () {
    const error = validateProfessional();
    if (error) {
      showError('bhw-info', error);
      return;
    }
    fillReview();
    showStep('bhw-review');
  };

  // "Edit" in the review step: jump to whichever step holds that field.
  window.bhwEdit = function (fieldId) {
    const field = $(fieldId);
    if (!field) return;
    const step = field.closest('.wizard-step');
    showStep(step.dataset.step);
    if (typeof field.focus === 'function') setTimeout(() => field.focus(), 300);
  };

  window.bhwSubmit = async function () {
    const form = $('registerForm');
    const button = $('bhwSubmitBtn');
    const originalText = button.textContent;

    button.disabled = true;
    button.textContent = 'Submitting...';

    try {
      const response = await fetch(form.action, { method: 'POST', body: new FormData(form) });
      const data = await response.json().catch(() => ({
        success: false,
        message: 'Something went wrong on the server. Please try again.',
      }));

      if (data.success) {
        showStep('bhw-pending');
      } else {
        showError('bhw-review', data.message || 'Registration failed. Please check your details.');
      }
    } catch (err) {
      showError('bhw-review', 'Could not reach the server. Check your connection and try again.');
    } finally {
      button.disabled = false;
      button.textContent = originalText;
    }
  };

  // ---------- Setup ----------

  document.addEventListener('DOMContentLoaded', () => {
    setupDropzone();

    const dob = $('bhw_dob');
    if (dob) dob.max = new Date().toISOString().split('T')[0];

    const role = $('role');
    if (role) {
      role.addEventListener('change', () => {
        syncDotCount();
        clearErrors();
      });
    }
    syncDotCount();
  });

  // Pressing Enter inside a BHW field would normally submit the whole form
  // through the family flow. Block that; BHW submits only via "Confirm & Submit".
  document.addEventListener(
    'submit',
    (e) => {
      if (e.target.id === 'registerForm' && isBhw()) {
        e.preventDefault();
        e.stopPropagation();
      }
    },
    true
  );
})();