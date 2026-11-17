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
    CHECK(ga_uart_feed(&p,'v',out)==1); CHECK(!strcmp(out,"v"));
    CHECK(ga_uart_feed(&p,'g',out)==0);
    for (int i=0;i<GA_COMMAND_MAX+10;i++) CHECK(ga_uart_feed(&p,'9',out)==0);
    CHECK(ga_uart_feed(&p,'\n',out)==-1);
    CHECK(ga_uart_feed(&p,'z',out)==1); CHECK(!strcmp(out,"z"));
    int n, values[6], v;
    CHECK(ga_parse_int("-100",&v) && v==-100);
    CHECK(!ga_parse_int("1oops",&v)); CHECK(!ga_parse_int("999999999999999999",&v));
    CHECK(!ga_parse_delays("1,,2",values,&n,6)); CHECK(!ga_parse_delays("1,",values,&n,6));
    CHECK(!ga_parse_delays("1,2,3,4,5,6,7",values,&n,6));
    CHECK(ga_parse_delays("90,117",values,&n,6) && n==2 && values[1]==117);
    campaign_cfg_t cfg = {.vd=354,.max_attempts=200,.oracle_mode=-1,.ndelays=1};
    CHECK(ga_campaign_options("start delay=90,117 max=1 mode=1",&cfg,err,sizeof err));
    CHECK(cfg.max_attempts==1 && cfg.oracle_mode==1 && cfg.ndelays==2 && cfg.delays[1]==117);
    campaign_cfg_t before=cfg;
    CHECK(!ga_campaign_options("vd=250 max=99999999999999999",&cfg,err,sizeof err));
    CHECK(!memcmp(&cfg,&before,sizeof cfg));
    CHECK(!ga_campaign_options("delays=1,2,3,4,5,6,7",&cfg,err,sizeof err));
    CHECK(!ga_campaign_options("unknown=1",&cfg,err,sizeof err));
    printf("protocol: %d checks, %d failures\n",checks,failures);
    return failures?1:0;
}
