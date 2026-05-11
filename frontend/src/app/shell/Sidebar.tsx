import { NavLink as RouterLink, useLocation } from 'react-router-dom'
import { Badge, Group, ScrollArea, Stack, Text } from '@mantine/core'
import clsx from 'clsx'
import { NAV_GROUPS } from './nav'
import styles from './Sidebar.module.css'

export function Sidebar() {
  const location = useLocation()
  return (
    <ScrollArea className={styles.sidebar} type="hover">
      <Stack gap="lg" p="md">
        <Group gap={8}>
          <div className={styles.logoDot} />
          <Text fw={700} size="sm">
            Sports Tail Trader
          </Text>
        </Group>
        {NAV_GROUPS.map((group) => (
          <Stack key={group.id} gap={4}>
            <Text className={styles.groupLabel}>{group.label}</Text>
            {group.links.map((link) => {
              const isActive =
                location.pathname === link.path || location.pathname.startsWith(`${link.path}/`)
              return (
                <RouterLink
                  key={link.path}
                  to={link.path}
                  end={link.path === '/live' || link.path === '/strategy'}
                  className={({ isActive: rlActive }) =>
                    clsx(styles.link, (rlActive || isActive) && styles.linkActive)
                  }
                >
                  <span>{link.label}</span>
                  {link.badge ? (
                    <Badge size="xs" variant="light" color="gray">
                      {link.badge}
                    </Badge>
                  ) : null}
                </RouterLink>
              )
            })}
          </Stack>
        ))}
      </Stack>
    </ScrollArea>
  )
}
