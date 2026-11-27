/* Real command/runtime code with all external effects replaced by local models.
 * This verifies parsing, mutual exclusion, failure cleanup and status publication;
 * it does not validate the ESP32 SDK, electrical behavior or injection timing. */
#include <assert.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include "../main/main.c"
#undef printf
static int checks,failures;
#define CHECK(c) do { checks++; if (!(c)) { failures++; printf("FAIL %d: %s\n",__LINE__,#c); } } while (0)
struct mock_mutex {int available;};
static struct mock_mutex locks[32];
static int nlocks,write_calls,read_calls,read_8b,fail_reg=-1,fail_read_reg=-1,task_core,task_priority;
/* Task creation is recorded per task name: the executor must stay core-pinned at its
 * priority, while the PEX hold guard is deliberately unpinned and lower. A single shared
 * "last task" variable would make adding any second task look like a regression. */
static int exec_core=-1,exec_priority=-1,guard_priority=-1,guard_pinned=-1;
static bool reset_low,bus_held,network_called;
static int64_t clock_us;
static esp_reset_reason_t reset_reason=ESP_RST_POWERON;
static uint32_t requested_baud=400000,actual_baud=400000;
static char log_text[8192],last_bus_command[GA_COMMAND_MAX];
static size_t log_used;
SemaphoreHandle_t xSemaphoreCreateMutex(void){assert(nlocks<32);locks[nlocks].available=1;return &locks[nlocks++];}
BaseType_t xSemaphoreTake(SemaphoreHandle_t m,TickType_t t){(void)t;if(!m->available)return 0;m->available=0;return 1;}
BaseType_t xSemaphoreGive(SemaphoreHandle_t m){assert(!m->available);m->available=1;return 1;}
void vTaskDelay(TickType_t t){clock_us+=(int64_t)t*1000;}
/* 0.5.8/0.5.9 加的临界区自旋与"读回实际优先级"在模型里的对应物。handle 回填是真的写回，
 * uxTaskPriorityGet 回的是创建时记下的那份 —— 于是 /api/status 里的 exec_prio 是被断言的，
 * 不是我们请求值的复读（请求值可以写错，实际值才是板上真相；同 baud 那条闸门一个道理）。 */
static TaskHandle_t s_exec_handle;
void vTaskSuspendAll(void){}
BaseType_t xTaskResumeAll(void){return 0;}
UBaseType_t uxTaskPriorityGet(const TaskHandle_t h){return h==s_exec_handle?(UBaseType_t)exec_priority:0;}
BaseType_t xTaskCreatePinnedToCore(void(*f)(void*),const char*n,uint32_t stack,void*a,UBaseType_t p,void*h,BaseType_t c)
 {(void)f;(void)stack;(void)a;task_core=c;task_priority=(int)p;
  if(n&&strcmp(n,"ga_executor")==0){exec_core=c;exec_priority=(int)p;
    if(h){*(TaskHandle_t*)h=(TaskHandle_t)&exec_priority;s_exec_handle=*(TaskHandle_t*)h;}}
  return pdPASS;}
BaseType_t xTaskCreate(void(*f)(void*),const char*n,uint32_t stack,void*a,UBaseType_t p,void*h)
{(void)f;(void)stack;(void)a;(void)h;task_priority=(int)p;
 if(n&&strcmp(n,"ga_pex_guard")==0){guard_priority=(int)p;guard_pinned=0;}return pdPASS;}
int64_t esp_timer_get_time(void){clock_us+=10;return clock_us;}
esp_err_t esp_timer_create(const esp_timer_create_args_t*c,esp_timer_handle_t*h){(void)c;*h=(void*)1;return ESP_OK;}
esp_err_t esp_timer_start_periodic(esp_timer_handle_t h,uint64_t n){(void)h;(void)n;return ESP_OK;}
esp_reset_reason_t esp_reset_reason(void){return reset_reason;}
void esp_restart(void){}
uint32_t esp_random(void){static uint32_t s=120;return ++s;}
esp_err_t gpio_set_direction(int p,int d){(void)p;(void)d;return ESP_OK;}
esp_err_t gpio_set_pull_mode(int p,int d){(void)p;(void)d;return ESP_OK;}
void console_init(void){}
int console_read_command(char*p,size_t n,int t){(void)p;(void)n;(void)t;return 0;}
int console_printf(const char*fmt,...){va_list a;va_start(a,fmt);int n=vsnprintf(log_text+log_used,sizeof log_text-log_used,fmt,a);va_end(a);if(n>0){size_t add=(size_t)n;if(add>=sizeof log_text-log_used)add=sizeof log_text-log_used-1;log_used+=add;}return n;}
void net_init(void){network_called=true;}
void net_wifi_cmd(const char*s){(void)s;}
void pex_init(void){reset_low=false;}
void pex_hold(bool low){reset_low=low;}
/* The guard's only input. Modelled from the pad state plus a clock the test winds: a
 * held pad that never comes back is precisely the forgotten-'j' failure. */
static int64_t pex_held_override_us;
int64_t pex_held_us(void){ return reset_low ? pex_held_override_us : 0; }
bool pmbus_up(void){return !bus_held;}
void pmbus_scl_hold(bool low){bus_held=low;}
/* The timeline guard (0.5.3). The firmware must arm it around the whole timeline, disarm
 * it on the way out, and be able to reopen the bus at the step that ends an intentional
 * SCL hold. seq_depth is checked from inside pmbus_w below, so a shot that reached the
 * bus did so with the guard armed; seq_reopens is how a hold_ms>0 plan says it came back. */
static int seq_depth,seq_max_depth,seq_reopens;
void pmbus_seq_begin(void){ if(++seq_depth>seq_max_depth) seq_max_depth=seq_depth; }
void pmbus_seq_end(void){ if(seq_depth>0) seq_depth--; }
bool pmbus_seq_reopen(void){ seq_reopens++; return !bus_held; }
uint32_t pmbus_baud_hz(void){return actual_baud;}
uint32_t pmbus_requested_baud_hz(void){return requested_baud;}
uint32_t pmbus_write_us(void){return glitch_pmbus_w_us(actual_baud);}
// The controller models its own write protect: while it is armed, every register
// the restore path needs nacks, and only the password clears it. Without this the
// firmware could pass a suite that never exercises the case where the VRM re-armed its
// protect between the last pulse and the hand-back.
//
// vrm_fault is the half that used to be missing, and the half that cost a day: a latched
// fault (exactly what our own over/under-voltage pulse produces) makes the part answer
// reads and *refuse the password*. Only CLEAR_FAULTS lets it in, and 0x03 is a plain
// command, not a protected write, so the write protect does not gate it. With the old
// password-first order this state is unreachable-from-itself: 20 no-op shots, and the
// bench concluded "the VRM latches solid, pull VDD33" -- which was wrong.
static int vrm_locked,vrm_fault;
static int seq_writes;   // writes that happened while the timeline guard was armed
/* Register file: the firmware writes config-id 0x46=0x1229 and reads it back, so a
 * fixture that answers every read with zero reports UNLOCK_CONFIGID on every shot.
 * Writes land here only when the controller accepts them, which is what makes the
 * read-back assertion mean something. */
static uint8_t regs[256];
/* devinit re-arm: the part ACKs the config-id write and keeps 0x1228 anyway. Only the
 * read-back catches this, which is why the firmware compares bytes instead of trusting ACK. */
static int cfg_devinit_locked;
static bool protected_reg(uint8_t r){return r==0x13||r==0x1F||r==0x21||r==0xE1||r==0x46;}
bool pmbus_w(uint8_t reg,const uint8_t*d,int n){
    write_calls++;clock_us+=100;if(seq_depth>0)seq_writes++;
    if(bus_held||reg==fail_reg) return false;
    if(reg==0x03){vrm_fault=0;return true;}                  // plain command, never gated
    if(vrm_locked&&protected_reg(reg)) return false;
    if(reg==0xF2){if(vrm_fault)return false;vrm_locked=0;}   // the password took
    if(cfg_devinit_locked&&reg==0x46) return true;           // ACKed, and not stored
    if(n>=1)regs[reg]=d[0];
    if(n>=2)regs[(uint8_t)(reg+1)]=d[1];
    return true;}
bool pmbus_w16(uint8_t r,uint16_t v){uint8_t d[2]={(uint8_t)v,(uint8_t)(v>>8)};return pmbus_w(r,d,2);}
bool pmbus_r(uint8_t reg,uint8_t*d,int n){memset(d,0,(size_t)n);clock_us+=100;read_calls++;if(reg==0x8b)read_8b++;
    if(reg==0x7e)d[0]=(vrm_locked?0:8)|(vrm_fault?0x20:0);
    /* 0.5.5 witnesses come from the register file too, so an assertion about them means the
     * firmware read the register and not the zero its memset would hand back. */
    if(reg==0x0b&&n>=2){d[0]=regs[0x0b];d[1]=regs[0x0c];}
    if(reg==0x79)d[0]=regs[0x79];
    if(reg==0x46&&n>=2){d[0]=cfg_devinit_locked?0x28:regs[0x46];d[1]=cfg_devinit_locked?0x12:regs[0x47];}
    return !bus_held && reg!=fail_read_reg;}
bool pmbus_raw_write(uint8_t a,const uint8_t*b,int n){(void)a;(void)b;(void)n;return false;}
bool pmbus_raw_read(uint8_t a,uint8_t*b,int n){(void)a;(void)b;(void)n;return false;}
void pmbus_cmd(char*s){snprintf(last_bus_command,sizeof last_bus_command,"%s",s);}

static void command(const char*s){char b[GA_COMMAND_MAX];snprintf(b,sizeof b,"%s",s);dispatch(b);}
int main(void)
{
    /* 执行器必须钉在 core 1 且**高于 esp_timer 任务（IDF 固定 22）**：本板每 50 ms 自己发一次
     * 心跳回调，那个回调就跑在 esp_timer 任务里；低于它，一次回调就能把计划步推后 100~200 µs。
     * （0.5.8 之前这里是 8 —— 那时我们把释放沿的散布归给抢占，后来逐 act 核对发现主因是阻塞式
     * 总线写；抬优先级仍然是对的，只是它治的不是那一样病。见 board.h 0.5.8/0.5.9 两段。） */
    ga_app_main();CHECK(exec_core==1 && exec_priority==24);
    {
        /* /api/status 报的是**从任务里读回来的**优先级，不是编译期常量：写错请求值时这条会红。 */
        char jsp[1024]; board_status_json(jsp, sizeof jsp);
        CHECK(strstr(jsp, "\"exec_prio\":24") != NULL);
        CHECK(strstr(jsp, "\"crit_spin_us\":3000") != NULL);
        CHECK(strstr(jsp, "\"reanchor\":1") != NULL);
    }
    /* The PEX hold guard must exist, be created, and sit below the executor: it exists so
     * a forgotten 'j' cannot leave the card in reset (see GA_PEX_MAX_HOLD_MS). */
    CHECK(guard_pinned==0 && guard_priority>=0 && guard_priority<exec_priority);
    CHECK(!board_is_busy());CHECK(!network_called);
    /* A forgotten 'j' must not be able to hold reset forever: on 2026-11-21 a 10 s manual
     * hold left the card unreachable and the box needed a power cycle, and nothing but a
     * person's memory distinguished that from a dead card. Drive it through the real
     * command path, then the guard, so the ceiling itself (not just the constant) is
     * what the build is testing. */
    command("j"); CHECK(reset_low);
    pex_held_override_us = (int64_t)GA_PEX_MAX_HOLD_MS * 1000 + 1;
    CHECK(pex_guard_once()); CHECK(!reset_low);
    command("j"); CHECK(reset_low);
    pex_held_override_us = 1000;                     /* plan-sized hold: must be left alone */
    CHECK(!pex_guard_once()); CHECK(reset_low);
    command("J"); CHECK(!reset_low);
    pex_held_override_us = 0;
    command("@F1000000");CHECK(!strcmp(last_bus_command,"F1000000"));
    command("@F400000");CHECK(!strcmp(last_bus_command,"F400000"));
    CHECK(board_begin_maintenance());CHECK(board_is_busy());CHECK(!board_begin_maintenance());
    int before=write_calls;command("g");CHECK(write_calls==before);
    command("@C start max=1");CHECK(!campaign_is_running());
    board_end_maintenance();CHECK(!board_is_busy());
    command("@C start delay=90,117 max=1 mode=1");CHECK(campaign_is_running());
    /* swin/revert (0.5.4): campaign.c used to pin both to 0, so a shot could never ask for the
     * only physical feedback this bench owns (0x8b rail samples) nor for the in-timeline restore
     * that survived 5/5 on 2026-11-24. Defaults must stay 0: all ~170 archived verdicts were
     * taken with both off, so a defaulted campaign has to be the same experiment. */
    campaign_cfg_t cc = campaign_defaults; char cerr[128];
    CHECK(ga_campaign_options("start delay=90 max=1 mode=1",&cc,cerr,sizeof cerr));
    CHECK(cc.swin==0&&cc.revert==0&&campaign_cfg_validate(&cc,cerr,sizeof cerr));
    cc = campaign_defaults;
    CHECK(ga_campaign_options("start delay=90 max=1 swin=1200 revert=1",&cc,cerr,sizeof cerr));
    CHECK(cc.swin==1200&&cc.revert==1&&campaign_cfg_validate(&cc,cerr,sizeof cerr));
    cc.swin=GLITCH_MAX_SAMPLE_US+1;CHECK(!campaign_cfg_validate(&cc,cerr,sizeof cerr));
    cc.swin=1200;cc.revert=2;CHECK(!campaign_cfg_validate(&cc,cerr,sizeof cerr));
    cc.revert=1;
    /* The keys must reach the shot, not just the config: a parameter that parses into a struct
     * nobody builds a timeline from would pass every other assertion here. */
    glitch_args_t sa;glitch_args_defaults(&sa);
    campaign_args_for(&cc,354,90,&sa);
    CHECK(sa.swin==1200&&sa.revert==1);
    command("r");CHECK(!reset_low);CHECK(write_calls==before);
    campaign_stop();campaign_poll();CHECK(!campaign_is_running());
    glitch_args_t a;glitch_args_defaults(&a);a.fast=0;a.revert=0;
    a.vd=-1;CHECK(!board_execute_glitch(&a,NULL,NULL));CHECK(write_calls==before);
    a.vd=325;
    int failed=-1;uint32_t seq=0;
    CHECK(board_execute_glitch(&a,&failed,&seq));CHECK(failed==0 && seq!=0 && s_last.valid);
    // The config-id is part of unlocking, not a follow-up nicety: without 0x46=0x1229 the
    // GPU's devinit re-locks the VRM after the first pulse releases PEX_RST, and every later
    // shot dies at the unlock gate. Assert the part actually holds the value (bus ACK alone
    // never proves a write landed), and that the firmware checks the read-back.
    CHECK(regs[0x46]==0x29&&regs[0x47]==0x12);
    CHECK(!reset_low && !board_is_busy());
    /* The timeline guard (0.5.3): every step's write ran with the guard armed, and the guard
     * is off again on the way out. This is the only thing standing between a bus that drops
     * mid-dip and a bring-up ladder inside the timeline -- one ladder is long enough to trip
     * the 250 ms glitch watchdog, which is how four campaigns on 2026-11-25 ended early, each
     * with the board reporting a phase it cannot even fire in. */
    CHECK(seq_max_depth==1 && seq_depth==0 && seq_writes>0);
    // A plan that holds SCL low takes the bus down on purpose; the step that ends the hold is
    // the one place a timeline is allowed to bring it back up.
    seq_writes=0;int reopens_before=seq_reopens;a.hold_ms=2;failed=-1;
    CHECK(board_execute_glitch(&a,&failed,&seq));CHECK(failed==0);
    CHECK(seq_reopens==reopens_before+1 && seq_depth==0);a.hold_ms=0;
    /* swin/revert must reach the timeline, not just the config. 0x8b samples taken during the
     * dip are the only physical evidence this bench has that a 222 us pit exists (a 16-bit
     * write occupies the bus for 104~320 us on real hardware, measured 2026-11-25), and
     * revert=1 is the restore shape that kept the card alive 5/5 on 2026-11-24. Both were
     * pinned to 0 by campaign.c, so neither was ever reachable from a fired shot. */
    int saw8b=read_8b;a.swin=1200;a.revert=1;failed=-1;
    CHECK(board_execute_glitch(&a,&failed,&seq));
    /* -2100..1200 每 500 us 一点 = 7 点，hold_ms=0 时一个都不该跳过 */
    CHECK(failed==0 && read_8b-saw8b==7);
    a.swin=0;int none8b=read_8b;
    CHECK(board_execute_glitch(&a,&failed,&seq));
    CHECK(read_8b==none8b);                       /* swin=0 必须真的不采 */
    int d0,d1,w0;
    a.swin=1200;a.revert=0;w0=write_calls;CHECK(board_execute_glitch(&a,&failed,&seq));d0=write_calls-w0;
    a.revert=1;w0=write_calls;CHECK(board_execute_glitch(&a,&failed,&seq));d1=write_calls-w0;
    CHECK(d1==d0+1);                              /* revert 只多一步：post+500ms 的 GA_MODE */
    a.swin=0;a.revert=0;
    fail_reg=0x21;CHECK(board_execute_glitch(&a,&failed,&seq));CHECK(failed>0);
    CHECK(!reset_low && !bus_held && !board_is_busy());fail_reg=-1;
    fail_read_reg=0x78;CHECK(!board_execute_glitch(&a,&failed,&seq));CHECK(!s_last.diag_ok);fail_read_reg=-1;
    char js[1024];board_status_json(js,sizeof js);CHECK(strstr(js,"\"diag_ok\":false")!=NULL);
    CHECK(strstr(js,"\"reset_timing\":\"software\"")!=NULL);
    /* 0.5.5: the two post-shot status registers are witnesses, not gates. STATUS_WORD(0x0B)
     * and STATUS_VOUT(0x79) carry the real VOUT_UV_FAULT (bit 4 of 0x79 — the old `vout_uv`
     * took bit 4 of 0x78, which is the CML bit), and this part is allowed not to implement
     * them. A clean shot must not become a failed one because a register we are only asking
     * answered nothing: that would kill every batch on a part that lacks the register. */
    fail_read_reg=0x0B;failed=-1;
    CHECK(board_execute_glitch(&a,&failed,&seq));CHECK(failed==0);
    fail_read_reg=0x79;failed=-1;
    CHECK(board_execute_glitch(&a,&failed,&seq));CHECK(failed==0);
    char js2[1024];board_status_json(js2,sizeof js2);
    CHECK(strstr(js2,"\"diag_ok\":true")!=NULL);
    CHECK(strstr(js2,"\"status_vout_ok\":false")!=NULL);    /* the NACK is visible, not swallowed */
    fail_read_reg=-1;failed=-1;
    regs[0x0b]=0x02;regs[0x0c]=0x80;regs[0x79]=0x10;         /* a latched UV, as the part would say */
    CHECK(board_execute_glitch(&a,&failed,&seq));CHECK(failed==0);
    char js3[1024];board_status_json(js3,sizeof js3);
    CHECK(strstr(js3,"\"status_word\":\"0x8002\"")!=NULL&&strstr(js3,"\"status_word_ok\":true")!=NULL);
    CHECK(strstr(js3,"\"status_vout\":\"0x10\"")!=NULL&&strstr(js3,"\"status_vout_ok\":true")!=NULL);
    regs[0x0b]=0;regs[0x0c]=0;regs[0x79]=0;
    struct {char out[4];char guard;} tiny={{0},'X'};board_status_json(tiny.out,sizeof tiny.out);CHECK(tiny.guard=='X');
    // A fault latch is the state our own pulse leaves behind, and it is not the same thing
    // as a dead part: it answers reads and refuses the password *only while the fault
    // stands*. GA_CLR lives in the timeline and the timeline is gated behind the password,
    // so asking the password first can never get out -- which is exactly how twenty
    // attempts went out as no-ops while PEX_RST never moved. CLEAR_FAULTS before the
    // password is the whole fix, and this is the test that keeps it fixed.
    vrm_locked=1;vrm_fault=1;fail_reg=-1;failed=-1;
    CHECK(board_execute_glitch(&a,&failed,&seq));CHECK(failed==0 && !reset_low);
    CHECK(!vrm_locked && !vrm_fault);
    vrm_locked=1;vrm_fault=0;failed=0;
    CHECK(board_execute_glitch(&a,&failed,&seq));CHECK(failed==0);   // plain protect, no fault
    // A part that really does refuse the password (not a fault) must not silently look like
    // a fired shot: no reset pulse, and a reported failure the host can act on.
    vrm_locked=1;fail_reg=0xF2;failed=0;
    CHECK(!board_execute_glitch(&a,&failed,&seq));CHECK(!reset_low);
    fail_reg=-1;vrm_locked=0;failed=0;
    CHECK(board_execute_glitch(&a,&failed,&seq));CHECK(failed==0);   // and it recovers on its own
    // config-id refused outright (write NACK) and config-id ACKed-but-not-stored (devinit
    // re-arm) are the two ways the unlock can be incomplete. Both must stop the shot before
    // PEX_RST moves: firing without 0x1229 is what produced a round of twelve where only the
    // first attempt was real, and the verdict cannot tell that apart.
    vrm_locked=1;fail_reg=0x46;failed=0;
    CHECK(!board_execute_glitch(&a,&failed,&seq));CHECK(!reset_low);
    fail_reg=-1;vrm_locked=0;failed=0;
    cfg_devinit_locked=1;
    CHECK(!board_execute_glitch(&a,&failed,&seq));CHECK(!reset_low);
    cfg_devinit_locked=0;failed=0;
    CHECK(board_execute_glitch(&a,&failed,&seq));CHECK(failed==0);   // recover
    CHECK(regs[0x46]==0x29&&regs[0x47]==0x12);
    s_fx.cookie=FX_COOKIE;s_fx.cookie_inv=~FX_COOKIE;s_fx.phase=PH_GLITCH_RUN;
    s_fx.magic=FX_MAGIC_WDT;s_fx.boot_count=7;reset_reason=ESP_RST_SW;
    s_fx.bite_phase=PH_GLITCH_RUN;s_fx.bite_us=251000;
    reset_history_init();CHECK(strstr(reset_note,"WATCHDOG") && strstr(reset_note,"phase=3"));
    CHECK(strstr(reset_note,"stale=251000us"));
    CHECK(s_fx.phase==PH_IDLE && s_fx.boot_count==8);
    // The lie this fixes: the app core rewrote `phase` to PREP while the restart was still
    // landing, so every bite on 2026-11-25 printed phase=2 -- a phase the watchdog cannot
    // fire in, which pointed us at code the watchdog was never in.
    s_fx.magic=FX_MAGIC_WDT;s_fx.phase=PH_GLITCH_PREP;s_fx.bite_phase=PH_GLITCH_RUN;
    s_fx.bite_us=300000;
    reset_history_init();CHECK(strstr(reset_note,"phase=3") && strstr(reset_note,"stale=300000us"));
    CHECK(s_fx.phase==PH_IDLE && s_fx.boot_count==9);   // both records counted, as before
    printf("runtime: %d checks, %d failures\n",checks,failures);return failures?1:0;
}
