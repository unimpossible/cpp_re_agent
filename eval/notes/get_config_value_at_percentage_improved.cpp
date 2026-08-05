uint32_t get_config_value_at_percentage(Undefined8 config_handle, int percentage) {
    uint32_t result_value = 0;
    uint8_t buffer[28];

    if (static_cast<int>(update_buffer_boundaries(config_handle)) == 0) {
        int clamped_pct = percentage;
        if (clamped_pct < 0) clamped_pct = 0;
        if (clamped_pct > 100) clamped_pct = 100;

        FUN_00109b2c(buffer, config_handle);

        const std::size_t buffer_len = FUN_00103bde(buffer);

        std::size_t target_idx = (buffer_len * static_cast<std::size_t>(clamped_pct) + 99) / 100;
        if (target_idx == 0) {
            target_idx = 1;
        }

        uint32_t* value_ptr = reinterpret_cast<uint32_t*>(FUN_00103f14(buffer, target_idx - 1));
        if (value_ptr != nullptr) {
            result_value = *value_ptr;
        }

        process_data_buffer(reinterpret_cast<long*>(buffer));
    }

    return result_value;
}