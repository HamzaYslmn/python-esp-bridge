// MCPWM: one complementary PWM pair with HW deadtime for H-bridge/half-bridge
// drivers (switches must never co-conduct). LEDC (mod_pwm) covers the rest.
// Absent on ESP32-S2/C3. IDF "prelude" driver, single instance.
#include "espbridge/protocol.h"
#include "espbridge/modules.h"

#if BRIDGE_HAS_MCPWM

#include <driver/mcpwm_prelude.h>

#define MCPWM_RES_HZ 10000000UL  // 10 MHz -> 100 ns ticks, deadtime_ns/100

static mcpwm_timer_handle_t timer = nullptr;
static mcpwm_oper_handle_t oper = nullptr;
static mcpwm_cmpr_handle_t cmp = nullptr;
static mcpwm_gen_handle_t gen_a = nullptr, gen_b = nullptr;
static uint32_t period_ticks = 0;

static void mcpwm_teardown() {
  if (timer) mcpwm_timer_disable(timer);
  if (gen_a) { mcpwm_del_generator(gen_a); gen_a = nullptr; }
  if (gen_b) { mcpwm_del_generator(gen_b); gen_b = nullptr; }
  if (cmp) { mcpwm_del_comparator(cmp); cmp = nullptr; }
  if (oper) { mcpwm_del_operator(oper); oper = nullptr; }
  if (timer) { mcpwm_del_timer(timer); timer = nullptr; }
}

void mcpwm_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len) {
  uint16_t cmd = CMD(MOD_MCPWM, op);
  switch (op) {
    case 0x01: {  // INIT: pin_a u8|pin_b i8 (-1 single)|freq u32|deadtime_ns u16
      if (len < 8) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      uint32_t freq = rd32(p + 2);
      if (freq == 0 || freq > MCPWM_RES_HZ / 4) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      mcpwm_teardown();
      period_ticks = MCPWM_RES_HZ / freq;
      uint32_t dt_ticks = (uint32_t)rd16(p + 6) / 100;  // ns -> 100 ns ticks

      mcpwm_timer_config_t tc = {};
      tc.group_id = 0;
      tc.clk_src = MCPWM_TIMER_CLK_SRC_DEFAULT;
      tc.resolution_hz = MCPWM_RES_HZ;
      tc.count_mode = MCPWM_TIMER_COUNT_MODE_UP;
      tc.period_ticks = period_ticks;
      mcpwm_operator_config_t oc = {};
      oc.group_id = 0;
      mcpwm_comparator_config_t cc = {};
      cc.flags.update_cmp_on_tez = true;
      if (mcpwm_new_timer(&tc, &timer) != ESP_OK
          || mcpwm_new_operator(&oc, &oper) != ESP_OK
          || mcpwm_operator_connect_timer(oper, timer) != ESP_OK
          || mcpwm_new_comparator(oper, &cc, &cmp) != ESP_OK) {
        mcpwm_teardown();
        proto_reply_err(seq, cmd, ST_IO);
        return;
      }
      mcpwm_generator_config_t ga = {};
      ga.gen_gpio_num = p[0];
      if (mcpwm_new_generator(oper, &ga, &gen_a) != ESP_OK) {
        mcpwm_teardown(); proto_reply_err(seq, cmd, ST_IO); return;
      }
      mcpwm_generator_set_action_on_timer_event(gen_a,
          MCPWM_GEN_TIMER_EVENT_ACTION(MCPWM_TIMER_DIRECTION_UP,
                                       MCPWM_TIMER_EVENT_EMPTY, MCPWM_GEN_ACTION_HIGH));
      mcpwm_generator_set_action_on_compare_event(gen_a,
          MCPWM_GEN_COMPARE_EVENT_ACTION(MCPWM_TIMER_DIRECTION_UP, cmp,
                                         MCPWM_GEN_ACTION_LOW));
      if ((int8_t)p[1] >= 0) {  // complementary output with deadtime
        mcpwm_generator_config_t gb = {};
        gb.gen_gpio_num = (int8_t)p[1];
        if (mcpwm_new_generator(oper, &gb, &gen_b) != ESP_OK) {
          mcpwm_teardown(); proto_reply_err(seq, cmd, ST_IO); return;
        }
        mcpwm_generator_set_action_on_timer_event(gen_b,
            MCPWM_GEN_TIMER_EVENT_ACTION(MCPWM_TIMER_DIRECTION_UP,
                                         MCPWM_TIMER_EVENT_EMPTY, MCPWM_GEN_ACTION_HIGH));
        mcpwm_generator_set_action_on_compare_event(gen_b,
            MCPWM_GEN_COMPARE_EVENT_ACTION(MCPWM_TIMER_DIRECTION_UP, cmp,
                                           MCPWM_GEN_ACTION_LOW));
        mcpwm_dead_time_config_t dta = {};
        dta.posedge_delay_ticks = dt_ticks;
        mcpwm_dead_time_config_t dtb = {};
        dtb.negedge_delay_ticks = dt_ticks;
        dtb.flags.invert_output = true;
        if (mcpwm_generator_set_dead_time(gen_a, gen_a, &dta) != ESP_OK
            || mcpwm_generator_set_dead_time(gen_a, gen_b, &dtb) != ESP_OK) {
          mcpwm_teardown(); proto_reply_err(seq, cmd, ST_IO); return;
        }
      }
      mcpwm_comparator_set_compare_value(cmp, 0);
      if (mcpwm_timer_enable(timer) != ESP_OK
          || mcpwm_timer_start_stop(timer, MCPWM_TIMER_START_NO_STOP) != ESP_OK) {
        mcpwm_teardown(); proto_reply_err(seq, cmd, ST_IO); return;
      }
      proto_reply_ok(seq, cmd);
      break;
    }
    case 0x02: {  // DUTY: permille u16 (0..1000)
      if (len < 2) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      if (!cmp) { proto_reply_err(seq, cmd, ST_NOT_INIT); return; }
      uint16_t pm = rd16(p);
      if (pm > 1000) pm = 1000;
      mcpwm_comparator_set_compare_value(cmp, (uint64_t)period_ticks * pm / 1000);
      proto_reply_ok(seq, cmd);
      break;
    }
    case 0x03:  // STOP (teardown; pins release)
      mcpwm_teardown();
      proto_reply_ok(seq, cmd);
      break;
    default:
      proto_reply_err(seq, cmd, ST_UNKNOWN_CMD);
  }
}

#else

UNSUPPORTED_STUB(mcpwm_handle, MOD_MCPWM)

#endif
