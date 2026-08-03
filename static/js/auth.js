function toggleBarangayField() {
    const role = document.getElementById('role').value;
    const barangayField = document.getElementById('barangayField');
    barangayField.style.display = role === 'bhw' ? 'block' : 'none';
}