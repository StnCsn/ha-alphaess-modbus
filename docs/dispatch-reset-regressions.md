# Reset and import/export limit corrections

The reset button now switches off every dispatch controller, removes its timers
and listeners, and drains queued controller work before sending the device reset.
Peak Shaving retains its existing lock-based stop implementation. Hold and PV
preferences remain unchanged. A failed device reset still propagates an error;
this change does not add communication-recovery retries or readback confirmation.

Force Import and Force Export now apply the selected inverter AC limit to their
initial calculation and parameter changes, as well as subsequent feedback updates.
The write path checks the limit again. For a 5 kW limit, a 5 kW export target and
1 kW house load, the requested battery discharge is capped at 5 kW instead of 6 kW.
The grid target may therefore be unattainable, which is preferable to requesting
power beyond the configured cap. The existing AC limit is used; this is not a
new firmware-specific battery capability calculation.

Run the focused tests without installing Home Assistant:

```
python -m unittest discover -s tests -p test_dispatch_controls.py -v
```

These import the real switch/button implementation using minimal HA stubs when
HA is absent. When HA is installed, including in the existing CI matrix, the
same tests use actual HA entity classes. The existing Peak Shaving tests remain
responsible for its controller-specific scenarios.

This patch is limited to the reset button, scheduled legacy-controller work and
import/export limits. It does not serialize all simultaneous external service
calls or certify the integration on hardware. Actual HA runtime, mocked Modbus,
firmware and equipment validation are still required before deployment.
