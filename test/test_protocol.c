#include <limits.h>
#include <stdio.h>
#include <string.h>
#include "protocol.h"
static int checks, failures;
#define CHECK(c) do { checks++; if (!(c)) { failures++; printf("FAIL %d: %s\n",__LINE__,#c); } } while (0)
int main(void)
{
    char out[GA_COMMAND_MAX], err[80];
    CHECK(ga_command_normalize("@F1000000",9,out)); CHECK(!strcmp(out,"@F1000000"));
    CHECK(ga_command_normalize(" v\r\n",4,out)); CHECK(!strcmp(out,"v"));
    CHECK(!ga_command_normalize("v\ng 1",5,out));
    CHECK(!ga_command_normalize("v z",3,out));
    CHECK(!ga_command_normalize("@",1,out));
    CHECK(!ga_command_normalize("g\0 1",4,out));
    ga_uart_parser_t p = {0};
    CHECK(ga_uart_feed(&p,'g',out)==0);
    CHECK(ga_uart_feed(&p,' ',out)==0);
    CHECK(ga_uart_feed(&p,'3',out)==0);
    CHECK(ga_uart_feed(&p,'2',out)==0);
    CHECK(ga_uart_feed(&p,'5',out)==0);
    CHECK(ga_uart_feed(&p,'\n',out)==1); CHECK(!strcmp(out,"g 325"));
    /* 单字符命令也得等换行。0.5.1 及之前是"收到就执行"，于是丢了头部的 JSON 心跳里，
     * 载荷中的 `r` 会当场脉冲 PEX_RST；现在它是错误，不是动作。 */
    CHECK(ga_uart_feed(&p,'v',out)==0);
    CHECK(ga_uart_feed(&p,'\n',out)==1); CHECK(!strcmp(out,"v"));
    CHECK(ga_uart_feed(&p,'g',out)==0);
    for (int i=0;i<GA_COMMAND_MAX+10;i++) CHECK(ga_uart_feed(&p,'9',out)==0);
    CHECK(ga_uart_feed(&p,'\n',out)==-1);
    CHECK(ga_uart_feed(&p,'z',out)==0);
    CHECK(ga_uart_feed(&p,'\n',out)==1); CHECK(!strcmp(out,"z"));

    /* 真机上真发生过的一次：突发丢了头部，快照正文自己成了"一行"。逐字节不得产生命令；
     * 整行结束时恰好一个错误。载荷里的 r/u/z/k/b 一个都不许被当成命令执行。 */
    static const char *frags[] = {
        "litch\",\"ledger\":{\"total\":112},\"r\":1,\"next\":{\"vd\":340}}",  /* 正文中段开头 */
        "r\",\"lost\":27},\"note\":\"keeper\"}",                               /* 以命令字母开头 */
    };
    for (size_t f = 0; f < sizeof frags / sizeof frags[0]; f++) {
        for (const char *t = frags[f]; *t; t++) CHECK(ga_uart_feed(&p,(unsigned char)*t,out)==0);
        CHECK(ga_uart_feed(&p,'\n',out)==-1);           /* 一片一次错误，不是每字节一次 */
    }
    /* 以 `g` 开头的片段确实会被当成一条 glitch 命令行（它本来就允许 `g …`），但它过不了
     * 参数解析：没有合法的 delay/vd 就发不出这一发。所以丢头部最多换来一行错误。 */
    const char *gfrag = "gates\":{\"card\":\"ok\"}";
    for (const char *t = gfrag; *t; t++) CHECK(ga_uart_feed(&p,(unsigned char)*t,out)==0);
    CHECK(ga_uart_feed(&p,'\n',out)==1); CHECK(!strcmp(out,gfrag));
    int n, values[6], v;
    CHECK(ga_parse_int("-100",&v) && v==-100);
    CHECK(!ga_parse_int("1oops",&v)); CHECK(!ga_parse_int("999999999999999999",&v));
    CHECK(!ga_parse_delays("1,,2",values,&n,6)); CHECK(!ga_parse_delays("1,",values,&n,6));
    CHECK(!ga_parse_delays("1,2,3,4,5,6,7",values,&n,6));
    CHECK(ga_parse_delays("90,117",values,&n,6) && n==2 && values[1]==117);
    campaign_cfg_t cfg = {.vd=354,.max_attempts=200,.ndelays=1};
    CHECK(ga_campaign_options("start delay=90,117 max=1 mode=1",&cfg,err,sizeof err));
    CHECK(cfg.max_attempts==1 && cfg.ndelays==2 && cfg.delays[1]==117);
    campaign_cfg_t before=cfg;
    /* 0.4.1: the board has no on-board oracle. mode=1 is accepted for old command
     * lines; mode=0 is refused with a message that says who will answer. */
    CHECK(!ga_campaign_options("mode=0",&cfg,err,sizeof err));
    CHECK(strstr(err,"mode removed")!=NULL && !memcmp(&cfg,&before,sizeof cfg));
    CHECK(!ga_campaign_options("vd=250 max=99999999999999999",&cfg,err,sizeof err));
    CHECK(!memcmp(&cfg,&before,sizeof cfg));
    CHECK(!ga_campaign_options("delays=1,2,3,4,5,6,7",&cfg,err,sizeof err));
    CHECK(!ga_campaign_options("unknown=1",&cfg,err,sizeof err));
    /* 0.5.0: the judge host's two line forms. `@V` accepts exactly the five words
     * /api/verdict accepts -- two transports with two vocabularies is how `lost` ends up
     * with a second meaning on the same bench. */
    uint32_t session, attempt, seq, adopted;
    oracle_t verdict;
    const char *json;
    CHECK(ga_verdict_line("12 3 under",&session,&attempt,&verdict,err,sizeof err));
    CHECK(session==12 && attempt==3 && verdict==ORACLE_UNDER);
    CHECK(ga_verdict_line("1   2   goldilocks",&session,&attempt,&verdict,err,sizeof err)
          && verdict==ORACLE_GOLDILOCKS);
    CHECK(ga_verdict_line("7 7 lost",&session,&attempt,&verdict,err,sizeof err)
          && verdict==ORACLE_LOST);
    CHECK(!ga_verdict_line("12 3 nojudge",&session,&attempt,&verdict,err,sizeof err));
    CHECK(!ga_verdict_line("0 3 under",&session,&attempt,&verdict,err,sizeof err));
    CHECK(!ga_verdict_line("12 under",&session,&attempt,&verdict,err,sizeof err));
    CHECK(!ga_verdict_line("12 3 under now",&session,&attempt,&verdict,err,sizeof err));
    CHECK(ga_agent_frame("seq=7 adopted=4 {\"state\":\"running\"}",&seq,&adopted,&json,err,sizeof err)
          && seq==7 && adopted==4 && !strcmp(json,"{\"state\":\"running\"}"));
    CHECK(ga_agent_frame("{\"state\":\"idle\"}",&seq,&adopted,&json,err,sizeof err)
          && seq==0 && adopted==0);        // both options are optional; a bare heartbeat counts
    CHECK(ga_agent_frame("{\"a\":\"delay=25,20\"}",&seq,&adopted,&json,err,sizeof err)
          && !strcmp(json,"{\"a\":\"delay=25,20\"}"));   // spaces inside the object survive
    CHECK(!ga_agent_frame("seq=7",&seq,&adopted,&json,err,sizeof err));
    CHECK(!ga_agent_frame("agent=1 {\"a\":1}",&seq,&adopted,&json,err,sizeof err));
    CHECK(!ga_agent_frame("seq=x {\"a\":1}",&seq,&adopted,&json,err,sizeof err));
    printf("protocol: %d checks, %d failures\n",checks,failures);
    return failures?1:0;
}
